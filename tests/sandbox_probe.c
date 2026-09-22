#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sched.h>
static void Arena_Fatal(void) { _exit(75); }
#include "../engine/sandbox.h"
#define DENIED(call) do { errno=0; if((call)!=-1 || errno!=EPERM) { fprintf(stderr,"Not denied: %s (errno=%d)\n",#call,errno); return 1; } } while(0)
int main(void) {
  struct rlimit lim;
  struct sched_param priority={0};
  int pidfd=syscall(SYS_pidfd_open,getpid(),0);
  if(pidfd<0 || syscall(SYS_pidfd_send_signal,pidfd,0,NULL,0) || getrlimit(RLIMIT_NOFILE,&lim)) return 2;
  Arena_Sandbox();
  DENIED(syscall(SYS_pidfd_send_signal,pidfd,0,NULL,0));
  DENIED(syscall(SYS_rt_sigqueueinfo,getpid(),0,NULL));
  DENIED(syscall(SYS_rt_tgsigqueueinfo,getpid(),getpid(),0,NULL));
  DENIED(syscall(SYS_prlimit64,getpid(),RLIMIT_NOFILE,NULL,&lim));
  DENIED(syscall(SYS_setpriority,PRIO_PROCESS,getpid(),0));
  DENIED(syscall(SYS_sched_setparam,getpid(),&priority));
  DENIED(syscall(SYS_sched_setscheduler,getpid(),SCHED_OTHER,&priority));
  DENIED(syscall(SYS_sched_setattr,getpid(),NULL,0));
  DENIED(socket(AF_INET,SOCK_STREAM,0));
  DENIED(kill(getpid(),0));
  errno=0;
  if(open("/etc/passwd",O_RDONLY)!=-1 || errno!=EACCES) return 3;
  puts("PASS: alternate process controls, sockets, signals and external files denied");
  return 0;
}
