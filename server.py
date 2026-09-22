"""Managed, private dedicated engines. Browsers never supply kill results."""

import asyncio
import fcntl
import hashlib
import json
import os
import platform
import socket
from pathlib import Path

from loguru import logger

from lnbits.settings import settings

from . import crud
from .models import MAPS, MAX_PLAYERS

ROOT = Path(__file__).resolve().parent


def prepare_binary():
    binary = ROOT / "bin" / "linux-x86_64" / "ioq3ded"
    manifest = json.loads(binary.with_name("manifest.json").read_text())
    if hashlib.sha256(binary.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("The packaged game engine failed its integrity check.")
    # Python's ZipFile extraction does not retain executable permissions.
    # Set only the owner's execute bit on this verified, bundled executable.
    mode = binary.stat().st_mode
    if not mode & 0o100:
        binary.chmod(mode | 0o100)
    return binary


class Match:
    def __init__(self, manager, arena, run):
        self.manager, self.arena, self.run = manager, arena, run
        self.lock = asyncio.Lock()
        self.peers = {}
        self.acks = {}
        self.ready = asyncio.Event()
        self.process = None
        self.reader = None
        self.output_task = None
        self.socket = None
        self.offset = 0
        self.last_used = crud.now()
        self.closed = False
        self.last_lease = 0
        self.journal = manager.directory / (run + ".jsonl")
        self.log = manager.directory / (run + ".log")

    async def start(self):
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        parent.setblocking(False)
        self.socket = parent
        # Paths and executable are local administrator-controlled files. No shell.
        binary = await asyncio.to_thread(prepare_binary)
        args = [
            str(binary),
            "+set",
            "fs_basepath",
            str(ROOT / "static" / "arena"),
            "+set",
            "fs_homepath",
            str(self.manager.directory / self.run),
            "+set",
            "com_basegame",
            "baseoa",
            "+set",
            "dedicated",
            "1",
            "+set",
            "vm_game",
            "1",
            "+set",
            "sv_maxclients",
            str(MAX_PLAYERS),
            "+set",
            "sv_fps",
            "30",
            "+set",
            "sv_pure",
            "1",
            "+set",
            "sv_allowDownload",
            "0",
            "+set",
            "sv_reconnectlimit",
            "0",
            "+set",
            "sv_maxRate",
            "25000",
            "+set",
            "sv_lanForceRate",
            "0",
            "+set",
            "bot_enable",
            "0",
            "+set",
            "g_gametype",
            "0",
            "+set",
            "fraglimit",
            "0",
            "+set",
            "timelimit",
            "0",
            "+set",
            "rconPassword",
            "",
            "+set",
            "g_log",
            "",
            "+set",
            "com_hunkMegs",
            "96",
            "+map",
            self.arena["map"],
        ]
        (self.manager.directory / self.run).mkdir(mode=0o700)
        self.journal.touch(mode=0o600, exist_ok=False)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                pass_fds=(child.fileno(), self.manager.worker_lock.fileno()),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.manager.directory,
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "TZ": "UTC",
                    "QUAKEJS_IPC_FD": str(child.fileno()),
                    "QUAKEJS_JOURNAL": str(self.journal),
                    "QUAKEJS_ASSETS": str(ROOT / "static" / "arena"),
                    "QUAKEJS_HOME": str(self.manager.directory / self.run),
                },
            )
            self.output_task = asyncio.create_task(self.capture_output())
        finally:
            child.close()
        self.reader = asyncio.create_task(self.receive())
        await asyncio.wait_for(self.ready.wait(), 30)
        if self.closed or self.process.returncode is not None:
            raise ValueError("The game server could not start.")

    async def capture_output(self):
        # Keep startup diagnostics; drain excess chat without growing log files.
        remaining = 1024 * 1024
        with self.log.open("wb") as output:
            os.chmod(self.log, 0o600)
            while chunk := await self.process.stdout.read(8192):
                if remaining:
                    saved = chunk[:remaining]
                    await asyncio.to_thread(output.write, saved)
                    remaining -= len(saved)

    async def send(self, data):
        if self.closed or not self.socket:
            raise ValueError("The game server is restarting.")
        # SEQPACKET writes are atomic. No unbounded socket buffer or send queue.
        try:
            if self.socket.send(data) != len(data):
                raise RuntimeError("Incomplete engine packet.")
        except BlockingIOError:
            if data[0] != 2:
                raise ValueError("Game server is busy. Please retry.") from None

    async def control(self, data):
        key = (data[0], data[1])
        future = asyncio.get_running_loop().create_future()
        self.acks[key] = future
        try:
            await self.send(data)
            await asyncio.wait_for(future, 3)
        finally:
            self.acks.pop(key, None)

    async def receive(self):
        try:
            while not self.closed:
                packet = await asyncio.get_running_loop().sock_recv(self.socket, 16386)
                if not packet:
                    break
                kind, peer = packet[:2]
                if kind == 6:
                    self.ready.set()
                elif kind in (1, 3, 7):
                    future = self.acks.get((kind, peer))
                    if future and not future.done():
                        future.set_result(None)
                elif kind == 2 and peer in self.peers:
                    connection = self.peers[peer]
                    connection.offer(packet[2:])
                # Death notifications are hints. Only the fsynced journal is read.
        except (OSError, ValueError):
            pass
        finally:
            if not self.closed:
                self.ready.set()
                self.closed = True
                for connection in list(self.peers.values()):
                    connection.offer(
                        {
                            "type": "error",
                            "message": "Game server disconnected. Reconnect to resume.",
                        }
                    )

    def read_events(self):
        if not self.journal.exists():
            raise RuntimeError("Missing game journal; admissions are stopped.")
        with self.journal.open("rb") as file:
            file.seek(self.offset)
            result = []
            for _ in range(256):
                line = file.readline(512)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    raise RuntimeError(
                        "Incomplete game journal; owner review required."
                    )
                result.append((file.tell(), json.loads(line)))
            return result

    async def replay(self):
        changed = False
        for offset, event in await asyncio.to_thread(self.read_events):
            await crud.consume_death(
                self.run, event["sequence"], event["victim"], event["killer"]
            )
            self.offset = offset
            for peer, connection in list(self.peers.items()):
                if connection.life and connection.life["id"] == event["victim"]:
                    if not self.closed:
                        await self.control(bytes([3, peer]))
                    self.peers.pop(peer, None)
                    connection.life = None
                    self.last_used = crud.now()
            changed = True
        return changed

    async def attach(self, connection):
        async with self.lock:
            await self.replay()
            # One game connection per participant. A second tab must not evict it.
            if any(
                c.token == connection.token and c is not connection
                for c in self.peers.values()
            ):
                raise ValueError("This player is already connected in another tab.")
            if connection.life:
                row = await crud.one(
                    "SELECT status FROM quakejs.lives WHERE id=:id",
                    id=connection.life["id"],
                )
                if row and row["status"] == "alive":
                    return connection.life
                await self.control(bytes([3, connection.life["slot"]]))
                await self.replay()
                self.peers.pop(connection.life["slot"], None)
            life = await crud.allocate_life(
                self.arena["id"], connection.token, self.run
            )
            try:
                await self.control(bytes([1, life["slot"]]) + life["id"].encode())
            except Exception:
                self.closed = True
                self.socket.close()
                # The manager observes closed and recovers the unused life.
                raise ValueError("Game server is restarting. Please retry.") from None
            connection.life, connection.match = life, self
            self.peers[life["slot"]] = connection
            self.last_used = crud.now()
            return life

    async def detach(self, connection):
        try:
            async with self.lock:
                life = connection.life
                if not life or self.peers.get(life["slot"]) is not connection:
                    return
                if not self.closed:
                    # The engine journals the forfeiture before acknowledging it.
                    # Its consumed guard makes a simultaneous frag spend only once.
                    await self.control(bytes([7, life["slot"]]))
                await self.replay()
                self.peers.pop(life["slot"], None)
                connection.life = None
                self.last_used = crud.now()
        except Exception:
            # A failed barrier must never leave an authorized orphan character.
            # Stop outside the match lock; recovery replays the durable journal.
            await self.stop()
            raise

    async def stop(self):
        self.closed = True
        if self.socket:
            self.socket.close()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.output_task:
            await asyncio.gather(self.output_task, return_exceptions=True)
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        async with self.lock:
            await self.replay()
            async with crud.transaction() as tx:
                await tx.lock_arena(self.arena["id"], active=False)
                await tx.execute(
                    "UPDATE quakejs.lives SET status='left',connected_until=0 "
                    "WHERE run_id=:id AND status='alive'",
                    id=self.run,
                )
                await tx.execute(
                    "UPDATE quakejs.runs SET status='stopped' WHERE id=:id", id=self.run
                )
                await tx.execute(
                    "UPDATE quakejs.arenas SET lease_until=0 WHERE id=:id AND "
                    "worker=:worker AND run_id=:run",
                    id=self.arena["id"],
                    worker=self.manager.worker,
                    run=self.run,
                )
        for connection in list(self.peers.values()):
            connection.offer(
                {
                    "type": "error",
                    "message": "Arena server stopped. Your unused lives are saved.",
                }
            )
        self.peers.clear()


class Manager:
    def __init__(self):
        self.worker = crud.uid()
        self.directory = Path(settings.lnbits_data_folder).resolve() / "quakejs"
        self.matches = {}
        self.blocked = set()
        self.connections = set()
        self.lock = asyncio.Lock()
        self.running = False
        self.max_matches = max(1, min(32, int(os.getenv("QUAKEJS_MAX_MATCHES", "4"))))
        self.worker_lock = None

    def check_available(self, arena_id):
        if not self.running:
            raise ValueError("QuakeJS is starting. Please retry shortly.")
        if arena_id in self.blocked:
            raise ValueError("This arena's ledger needs owner review.")
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("This package requires a Linux x86-64 server.")

    async def ensure(self, arena_id):
        async with self.lock:
            self.check_available(arena_id)
            if arena_id in self.matches and not self.matches[arena_id].closed:
                return self.matches[arena_id]
            if arena_id in self.matches:
                await self.matches.pop(arena_id).stop()
            if len(self.matches) >= self.max_matches:
                raise ValueError("The game server is at capacity. Try again later.")
            run = crud.uid()
            async with crud.transaction() as tx:
                arena = await tx.lock_arena(arena_id)
                if arena["map"] not in {m["value"] for m in MAPS}:
                    raise ValueError("Map not installed.")
                if arena["lease_until"] > crud.now():
                    raise ValueError(
                        "This arena is served by another worker. "
                        "Use a single LNbits worker."
                    )
                # A previous worker's journal must be recovered before its lives move.
                old_runs = await tx.all(
                    "SELECT * FROM quakejs.runs "
                    "WHERE arena_id=:id AND status='running'",
                    id=arena_id,
                )
                await tx.execute(
                    "UPDATE quakejs.arenas SET "
                    "worker=:worker,run_id=:run,lease_until=:until WHERE id=:id",
                    id=arena_id,
                    worker=self.worker,
                    run=run,
                    until=crud.now() + 45,
                )
            for old in old_runs:
                recovered = Match(self, arena, old["id"])
                await recovered.replay()
                async with crud.transaction() as tx:
                    await tx.execute(
                        "UPDATE quakejs.lives SET "
                        "status='left',connected_until=0 WHERE run_id=:run AND "
                        "status='alive'",
                        run=old["id"],
                    )
                    await tx.execute(
                        "UPDATE quakejs.runs SET status='recovered' WHERE id=:run",
                        run=old["id"],
                    )
            async with crud.transaction() as tx:
                await tx.execute(
                    "INSERT INTO quakejs.runs(id,arena_id,worker,created_at) "
                    "VALUES(:id,:arena,:worker,:now)",
                    id=run,
                    arena=arena_id,
                    worker=self.worker,
                    now=crud.now(),
                )
            match = Match(self, arena, run)
            self.matches[arena_id] = match
            try:
                await match.start()
            except Exception:
                await match.stop()
                self.matches.pop(arena_id, None)
                logger.warning("QuakeJS dedicated server failed to start: {}", run)
                raise ValueError(
                    "Game server unavailable. No entry payment is needed; contact "
                    "the arena owner."
                ) from None
            return match

    async def notify(self, arena_id):
        for connection in list(self.connections):
            if connection.arena_id == arena_id:
                connection.dirty.set()

    async def renew(self, match, arena_id):
        if crud.now() - match.last_lease < 5:
            return
        async with crud.transaction() as tx:
            arena = await tx.lock_arena(arena_id, active=False)
            if (
                not arena["active"]
                or arena["worker"] != self.worker
                or arena["lease_until"] <= crud.now()
            ):
                raise ValueError("Arena lease ended.")
            await tx.execute(
                "UPDATE quakejs.arenas SET lease_until=:until WHERE id=:id",
                id=arena_id,
                until=crud.now() + 45,
            )
            for connection in list(match.peers.values()):
                if connection.life:
                    await tx.execute(
                        "UPDATE quakejs.lives SET connected_until=:until WHERE "
                        "id=:id AND status='alive'",
                        id=connection.life["id"],
                        until=crud.now() + 30,
                    )
        match.last_lease = crud.now()

    async def loop(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.worker_lock = (self.directory / "worker.lock").open("a+b")
        try:
            fcntl.flock(self.worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.worker_lock.close()
            raise RuntimeError(
                "QuakeJS requires one LNbits worker per data directory."
            ) from None
        self.running = True
        try:
            while True:
                for arena_id, match in list(self.matches.items()):
                    async with self.lock:
                        if self.matches.get(arena_id) is not match:
                            continue
                        try:
                            async with match.lock:
                                changed = await match.replay()
                                await self.renew(match, arena_id)
                            if changed:
                                await self.notify(arena_id)
                            if match.closed or (
                                not match.peers and crud.now() - match.last_used > 300
                            ):
                                await match.stop()
                                self.matches.pop(arena_id, None)
                        except Exception:
                            logger.warning(
                                "QuakeJS stopped an arena to protect "
                                "its life ledger: {}",
                                arena_id,
                            )
                            try:
                                await match.stop()
                            except Exception:
                                # Keep corrupt/unreadable journals for review without
                                # terminating other independent arena processes.
                                self.blocked.add(arena_id)
                                for connection in list(match.peers.values()):
                                    connection.offer(
                                        {
                                            "type": "error",
                                            "message": "Arena stopped. Its ledger "
                                            "needs owner review; do not pay again.",
                                        }
                                    )
                            finally:
                                self.matches.pop(arena_id, None)
                await asyncio.sleep(0.1)
        finally:
            self.running = False
            await asyncio.gather(
                *(match.stop() for match in self.matches.values()),
                return_exceptions=True,
            )
            self.matches.clear()
            self.worker_lock.close()
            self.worker_lock = None


manager = Manager()
