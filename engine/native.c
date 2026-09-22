/* QuakeJS dedicated server integration. GPL-2.0-or-later.
 * No listening sockets. Only the parent process can authorize a virtual peer.
 * Deaths are journaled and fsynced before notification; stdout is never trusted.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "../server/server.h"
#include <sys/socket.h>
#include <sys/stat.h>
#include <poll.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <stdlib.h>
#include <stdio.h>

#define ARENA_PEERS 8
#define ARENA_ID 48
static int arena_fd = -1, journal_fd = -1;
static unsigned long arena_sequence;
static char admissions[ARENA_PEERS + 1][ARENA_ID + 1];
static char clients[MAX_CLIENTS][ARENA_ID + 1];
static int client_peer[MAX_CLIENTS];
static qboolean consumed[ARENA_PEERS + 1], claimed[ARENA_PEERS + 1];

static void Arena_Fatal(void) { _exit(75); }
#include "arena_sandbox.h"
static qboolean Arena_Id(const char *id) {
  int i;
  if (strlen(id) != ARENA_ID) return qfalse;
  for (i = 0; i < ARENA_ID; i++) if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f'))) return qfalse;
  return qtrue;
}
static int Arena_Peer(netadr_t from) {
  if (from.type != NA_IP || from.ip[0] != 127 || from.ip[1] || from.ip[2]) return 0;
  return from.ip[3] >= 1 && from.ip[3] <= ARENA_PEERS ? from.ip[3] : 0;
}
static void Arena_Ack(const void *packet, int length) {
  if (send(arena_fd, packet, length, MSG_NOSIGNAL) != length) Arena_Fatal();
}
void Arena_Ready(void) { const unsigned char packet[2] = {6, 0}; Arena_Ack(packet, 2); }
void Arena_Init(void) {
  const char *fd = getenv("QUAKEJS_IPC_FD"), *path = getenv("QUAKEJS_JOURNAL");
  if (!fd || !path) Arena_Fatal();
  arena_fd = atoi(fd);
  if (arena_fd < 3 || fcntl(arena_fd, F_SETFL, O_NONBLOCK) < 0) Arena_Fatal();
  journal_fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (journal_fd < 0) Arena_Fatal();
  Arena_Sandbox();
}
qboolean Arena_Admit(netadr_t from, const char *id) {
  int peer = Arena_Peer(from);
  return peer && !consumed[peer] && !claimed[peer] && Arena_Id(id) && !strcmp(admissions[peer], id);
}
void Arena_Client(int slot, netadr_t from) {
  int peer = Arena_Peer(from);
  if (slot < 0 || slot >= MAX_CLIENTS || !peer) Arena_Fatal();
  Q_strncpyz(clients[slot], admissions[peer], sizeof(clients[slot]));
  client_peer[slot] = peer; claimed[peer] = qtrue;
}
void Arena_Send(int length, const void *data, netadr_t to) {
  unsigned char packet[MAX_MSGLEN + 2];
  int peer = Arena_Peer(to);
  if (!peer || length < 1 || length > MAX_MSGLEN) return;
  packet[0] = 2; packet[1] = peer;
  memcpy(packet + 2, data, length);
  if (send(arena_fd, packet, length + 2, MSG_NOSIGNAL) < 0 && errno != EAGAIN && errno != EWOULDBLOCK) Arena_Fatal();
}
static void Arena_EndLife(int peer, const char *killer_id) {
  int size;
  char line[256], message[258];
  if (peer < 1 || peer > ARENA_PEERS || consumed[peer] || !admissions[peer][0]) return;
  consumed[peer] = qtrue;
  size = snprintf(line, sizeof(line), "{\"sequence\":%lu,\"killer\":\"%s\",\"victim\":\"%s\"}\n", ++arena_sequence, killer_id, admissions[peer]);
  if (size < 0 || size >= sizeof(line) || write(journal_fd, line, size) != size || fsync(journal_fd)) Arena_Fatal();
  message[0] = 4; message[1] = 0; memcpy(message + 2, line, size);
  /* A notification may be dropped under pressure; the journal is authoritative. */
  if (send(arena_fd, message, size + 2, MSG_NOSIGNAL) < 0 && errno != EAGAIN && errno != EWOULDBLOCK) Arena_Fatal();
}
void Arena_Disconnect(int slot) {
  if (slot >= 0 && slot < MAX_CLIENTS) Arena_EndLife(client_peer[slot], "");
}
void Arena_Frag(const char *event) {
  int killer = -1, victim = -1, peer;
  const char *killer_id = "";
  if (sscanf(event, "%d %d", &killer, &victim) != 2 || victim < 0 || victim >= MAX_CLIENTS) return;
  peer = client_peer[victim];
  if (!peer || !clients[victim][0] || strcmp(admissions[peer], clients[victim])) return;
  if (killer >= 0 && killer < MAX_CLIENTS && killer != victim) killer_id = clients[killer];
  Arena_EndLife(peer, killer_id);
}
static void Arena_Revoke(int peer) {
  int slot;
  consumed[peer] = qtrue;
  admissions[peer][0] = 0;
  for (slot = 0; slot < MAX_CLIENTS; slot++) if (client_peer[slot] == peer) {
    if (slot < sv_maxclients->integer && svs.clients[slot].state >= CS_CONNECTED) SV_DropClient(&svs.clients[slot], "Admission ended");
    clients[slot][0] = 0; client_peer[slot] = 0;
  }
}
void Arena_Sleep(int msec) {
  unsigned char packet[MAX_MSGLEN + 2];
  struct pollfd wait = {arena_fd, POLLIN, 0};
  int length, peer, count = 0;
  if (arena_fd < 0) Arena_Fatal();
  if (msec > 10) msec = 10;
  if (msec < 0) msec = 0;
  poll(&wait, 1, msec);
  if (wait.revents & (POLLHUP | POLLERR | POLLNVAL)) Arena_Fatal();
  while (count++ < 256 && (length = recv(arena_fd, packet, sizeof(packet), MSG_DONTWAIT)) > 0) {
    if (length < 2 || packet[1] < 1 || packet[1] > ARENA_PEERS) continue;
    peer = packet[1];
    if (packet[0] == 1 && length == ARENA_ID + 2) {
      char id[ARENA_ID + 1];
      memcpy(id, packet + 2, ARENA_ID); id[ARENA_ID] = 0;
      if (!Arena_Id(id)) continue;
      Arena_Revoke(peer);
      Q_strncpyz(admissions[peer], id, sizeof(admissions[peer])); consumed[peer] = qfalse; claimed[peer] = qfalse;
      Arena_Ack(packet, length);
    } else if (packet[0] == 7 && length == 2) {
      Arena_EndLife(peer, ""); Arena_Revoke(peer); Arena_Ack(packet, 2);
    } else if (packet[0] == 3) { Arena_Revoke(peer); Arena_Ack(packet, 2); }
    else if (packet[0] == 2 && admissions[peer][0] && !consumed[peer]) {
      netadr_t from = {0}; msg_t msg;
      from.type = NA_IP; from.ip[0] = 127; from.ip[3] = peer; from.port = BigShort(PORT_SERVER);
      MSG_Init(&msg, packet + 2, sizeof(packet) - 2); msg.cursize = length - 2;
      if (com_sv_running->integer) Com_RunAndTimeServerPacket(&from, &msg);
    }
  }
}
