import {readFile, writeFile, copyFile} from 'node:fs/promises'
import {resolve, dirname} from 'node:path'
const root=resolve(process.argv[2]), here=import.meta.dirname
async function patch(file,before,after) {
 const path=resolve(root,file), source=await readFile(path,'utf8')
 if(!source.includes(before))throw Error('Upstream changed: '+file)
 await writeFile(path,source.replace(before,after))
}
for(const name of ['native.c','native.h','sandbox.h']) await copyFile(resolve(here,name),resolve(root,'code/qcommon/arena_'+name))
await patch('cmake/server.cmake','list(APPEND SERVER_DEFINITIONS DEDICATED)','list(APPEND SERVER_DEFINITIONS DEDICATED QUAKEJS_NATIVE)\nlist(APPEND SERVER_BINARY_SOURCES ${SOURCE_DIR}/qcommon/arena_native.c)')
for(const file of ['code/server/sv_client.c','code/server/sv_game.c','code/server/sv_init.c']) await patch(file,'#include "server.h"','#include "server.h"\n#ifdef QUAKEJS_NATIVE\n#include "../qcommon/arena_native.h"\n#endif')
await patch('code/server/sv_client.c','\tversion = atoi(Info_ValueForKey(userinfo, "protocol"));',`#ifdef QUAKEJS_NATIVE
  if (!Arena_Admit(from, Info_ValueForKey(userinfo, "lnbits_id"))) {
    NET_OutOfBandPrint(NS_SERVER, from, "print\\nA live paid admission is required.\\n"); return;
  }
#endif
\tversion = atoi(Info_ValueForKey(userinfo, "protocol"));`)
await patch('code/server/sv_client.c','\tSV_UserinfoChanged( newcl );',`#ifdef QUAKEJS_NATIVE
  Arena_Client(clientNum, newcl->netchan.remoteAddress);
#endif
\tSV_UserinfoChanged( newcl );`)
await patch('code/server/sv_game.c','\tcase G_CVAR_SET:',`\tcase G_CVAR_SET:
#ifdef QUAKEJS_NATIVE
    if (!strcmp((const char *)VMA(1), "lnbits_frag")) { Arena_Frag((const char *)VMA(2)); return 0; }
#endif`)
await patch('code/qcommon/net_ip.c','#include "../qcommon/qcommon.h"','#include "../qcommon/qcommon.h"\n#ifdef QUAKEJS_NATIVE\n#include "arena_native.h"\n#endif')
await patch('code/qcommon/net_ip.c','void NET_Init( void ) {','void NET_Init( void ) {\n#ifdef QUAKEJS_NATIVE\n  Cvar_Set("net_enabled", "0"); NET_Config(qfalse); Arena_Init(); return;\n#endif')
await patch('code/qcommon/net_ip.c','void NET_Sleep(int msec)\n{','void NET_Sleep(int msec)\n{\n#ifdef QUAKEJS_NATIVE\n  Arena_Sleep(msec); return;\n#endif')
// Standard UDP is never opened; every packet is routed over the private IPC fd.
await patch('code/qcommon/net_ip.c','void Sys_SendPacket( int length, const void *data, netadr_t to ) {','void Sys_SendPacket( int length, const void *data, netadr_t to ) {\n#ifdef QUAKEJS_NATIVE\n  Arena_Send(length, data, to); return;\n#endif')

await patch('code/server/sv_init.c','\tsv.state = SS_GAME;','\tsv.state = SS_GAME;\n#ifdef QUAKEJS_NATIVE\n  Arena_Ready();\n#endif')

await patch("code/game/g_client.c", "\t// stop any following clients", "  // Remove projectiles before this entity slot can belong to another paid life.\n  // Otherwise an old rocket could be credited to the replacement occupant.\n  for (i = MAX_CLIENTS; i < level.num_entities; i++) {\n    if (g_entities[i].inuse && g_entities[i].s.eType == ET_MISSILE &&\n        g_entities[i].parent == ent) G_FreeEntity(&g_entities[i]);\n  }\n\t// stop any following clients")

// All client departures, including an in-game disconnect or timeout, end the life.
// Administrative revocation marks it consumed first, so cleanup cannot spend twice.
await patch('code/server/sv_client.c', '\t// Free all allocated data on the client structure', '#ifdef QUAKEJS_NATIVE\n  Arena_Disconnect(drop - svs.clients);\n#endif\n\t// Free all allocated data on the client structure')
