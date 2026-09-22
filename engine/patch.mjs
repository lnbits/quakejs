import {readFile, writeFile} from 'node:fs/promises'
import {resolve} from 'node:path'

const root = resolve(process.argv[2])
async function patch(file, before, after) {
  const path = resolve(root, file)
  const source = await readFile(path, 'utf8')
  if (!source.includes(before)) throw new Error(`Upstream changed: ${file}`)
  await writeFile(path, source.replace(before, after))
}

await patch('cmake/platforms/emscripten.cmake', '-sEXPORT_ES6', `-sWASM=0
    -sDYNAMIC_EXECUTION=0
    -sSINGLE_FILE=1
    -sMODULARIZE=1
    -sENVIRONMENT=web
    -sALLOW_MEMORY_GROWTH=1`)
await patch('cmake/platforms/emscripten.cmake', 'set(BUILD_RENDERER_GL1 OFF CACHE INTERNAL "")', 'set(BUILD_RENDERER_GL1 OFF CACHE INTERNAL "")\nset(BUILD_RENDERER_GL2 ON CACHE INTERNAL "")')
await patch('cmake/platforms/emscripten.cmake', '-sEXPORTED_FUNCTIONS=_main,_Cbuf_AddText,_free', '-sEXPORTED_FUNCTIONS=_main,_Cbuf_AddText,_free,_Arena_TouchLook')
await patch('cmake/identity.cmake', 'set(CLIENT_NAME ioquake3)', 'set(CLIENT_NAME QuakeArena)')
await patch('cmake/identity.cmake', 'set(BASEGAME baseq3)', 'set(BASEGAME baseoa)')

await patch('code/server/sv_client.c', '#include "server.h"', `#include "server.h"
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
EM_JS(int, Arena_Admit, (int peer, const char *id), {
  return Module.arenaAdmit ? +Module.arenaAdmit(peer, UTF8ToString(id)) : 0;
});
EM_JS(void, Arena_Client, (int slot, const char *id), {
  if (Module.arenaClient) Module.arenaClient(slot, UTF8ToString(id));
});
#endif`)
await patch('code/server/sv_client.c', '\tversion = atoi(Info_ValueForKey(userinfo, "protocol"));', `#ifdef __EMSCRIPTEN__
  if (!Arena_Admit(from.type == NA_LOOPBACK ? -1 : from.type == NA_WEBRTC ? from.ip[0] : -2,
                   Info_ValueForKey(userinfo, "lnbits_id"))) {
    NET_OutOfBandPrint(NS_SERVER, from, "print\\nA live paid admission is required.\\n");
    return;
  }
#endif
\tversion = atoi(Info_ValueForKey(userinfo, "protocol"));`)
await patch('code/server/sv_client.c', '\tSV_UserinfoChanged( newcl );', `#ifdef __EMSCRIPTEN__
  Arena_Client(clientNum, Info_ValueForKey(userinfo, "lnbits_id"));
#endif
\tSV_UserinfoChanged( newcl );`)

await patch('code/server/sv_game.c', '#include "server.h"', `#include "server.h"
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
EM_JS(void, Arena_Frag, (const char *event), {
  if (Module.arenaFrag) Module.arenaFrag(UTF8ToString(event));
});
#endif`)
await patch('code/server/sv_game.c', '\tcase G_CVAR_SET:', `\tcase G_CVAR_SET:
#ifdef __EMSCRIPTEN__
    if (!strcmp((const char *)VMA(1), "lnbits_frag")) {
      Arena_Frag((const char *)VMA(2));
      return 0;
    }
#endif`)
await patch('code/game/g_combat.c', '\t// broadcast the death event to everyone', `  // Only the server game VM can emit a payable frag.
  trap_Cvar_Set("lnbits_frag", va("%i %i", killer, self->s.number));
\t// broadcast the death event to everyone`)
await patch('code/game/g_active.c', '\t\t// wait for the attack button to be pressed', `    // A paid life is consumed on death. Only a fresh admission may respawn.
    return;
\t\t// wait for the attack button to be pressed`)
await patch('code/game/g_cmds.c', '\tif (Q_stricmp (cmd, "say") == 0) {', `  // Team changes and map votes must not bypass paid-life consumption.
  if (!Q_stricmp(cmd, "team") || !Q_stricmp(cmd, "follow") ||
      !Q_stricmp(cmd, "follownext") || !Q_stricmp(cmd, "followprev") ||
      !Q_stricmp(cmd, "callvote") || !Q_stricmp(cmd, "callteamvote")) return;
\tif (Q_stricmp (cmd, "say") == 0) {`)
await patch('code/client/cl_cgame.c', '#include "client.h"', `#include "client.h"
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif`)
await patch('code/client/cl_cgame.c', '\tclc.state = CA_ACTIVE;', `\tclc.state = CA_ACTIVE;
#ifdef __EMSCRIPTEN__
  EM_ASM({ if (Module.arenaReady) Module.arenaReady(); });
#endif`)
await patch('code/client/cl_main.c', '\tif ( com_sv_running->integer && !strcmp( server, "localhost" ) ) {', `#ifdef __EMSCRIPTEN__
  // A fresh paid local admission must retain the browser's listen server.
  if (strcmp(server, "localhost")) {
#endif
\tif ( com_sv_running->integer && !strcmp( server, "localhost" ) ) {`)
await patch('code/client/cl_main.c', '\tSV_Frame( 0 );\n\n\tnoGameRestart = qtrue;', `\tSV_Frame( 0 );
#ifdef __EMSCRIPTEN__
  }
#endif
\n\tnoGameRestart = qtrue;`)
await patch('code/client/cl_main.c', '\tCvar_Set( "sv_cheats", "1" );', `\tCvar_Set( "sv_cheats", "1" );
#ifdef __EMSCRIPTEN__
  if (com_sv_running->integer) Cvar_Set("sv_cheats", "0");
#endif`)
await patch('code/sdl/sdl_input.c', '#include "../client/client.h"', `#include "../client/client.h"
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
EM_JS(int, Arena_InputEnabled, (void), {
  return Module.arenaInputEnabled ? +Module.arenaInputEnabled() : 0;
});
EM_JS(int, Arena_TouchMode, (void), {
  return Module.arenaTouchControls ? 1 : 0;
});
EMSCRIPTEN_KEEPALIVE void Arena_TouchLook(int dx, int dy) {
  if (Arena_TouchMode() && Arena_InputEnabled())
    Com_QueueEvent(Sys_Milliseconds(), SE_MOUSE, dx, dy, 0, NULL);
}
#endif`)
await patch('code/sdl/sdl_input.c', '\tif( !cls.glconfig.isFullscreen && ( Key_GetCatcher( ) & KEYCATCH_CONSOLE ) )', `#ifdef __EMSCRIPTEN__
  if (!Arena_InputEnabled()) {
    IN_DeactivateMouse(qfalse);
    Key_ClearStates();
  } else if (Arena_TouchMode()) {
    // Touch deltas enter through Arena_TouchLook; mobile needs no pointer lock.
    IN_DeactivateMouse(qfalse);
  } else
#endif
\tif( !cls.glconfig.isFullscreen && ( Key_GetCatcher( ) & KEYCATCH_CONSOLE ) )`)
await patch('code/sdl/sdl_glimp.c', '\t\t\tif( ( SDL_window = SDL_CreateWindow', `#ifdef __EMSCRIPTEN__
      SDL_SetHint(SDL_HINT_EMSCRIPTEN_KEYBOARD_ELEMENT, "#canvas");
#endif
\t\t\tif( ( SDL_window = SDL_CreateWindow`)
