/* QuakeJS private, parent-owned transport. GPL-2.0-or-later. */
void Arena_Init(void);
void Arena_Sleep(int msec);
void Arena_Send(int length, const void *data, netadr_t to);
qboolean Arena_Admit(netadr_t from, const char *id);
void Arena_Client(int slot, netadr_t from);
void Arena_Frag(const char *event);

void Arena_Ready(void);

void Arena_Disconnect(int slot);
