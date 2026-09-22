/* Linux process confinement for the network-facing engine. GPL-2.0-or-later. */
#define _GNU_SOURCE
#include <linux/landlock.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/audit.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/resource.h>
#include <stddef.h>

static void Arena_PathRule(int ruleset, const char *path, unsigned long long access) {
  struct landlock_path_beneath_attr rule = {0};
  rule.allowed_access = access;
  rule.parent_fd = open(path, O_PATH | O_CLOEXEC);
  if (rule.parent_fd < 0 || syscall(SYS_landlock_add_rule, ruleset, LANDLOCK_RULE_PATH_BENEATH, &rule, 0)) Arena_Fatal();
  close(rule.parent_fd);
}
#define DENY_SYSCALL(name) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, SYS_##name, 0, 1), BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO|EPERM)
static void Arena_Sandbox(void) {
  const char *assets=getenv("QUAKEJS_ASSETS"), *home=getenv("QUAKEJS_HOME");
  int abi, ruleset;
  unsigned long long read_access=LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR;
  unsigned long long access=(1ULL << 13)-1;
  struct landlock_ruleset_attr rules = {0};
  struct rlimit memory={512UL*1024*1024,512UL*1024*1024}, core={0,0}, files={64,64}, size={16UL*1024*1024,16UL*1024*1024};
  struct sock_filter filter[] = {
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data,arch)),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,AUDIT_ARCH_X86_64,1,0),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
    BPF_JUMP(BPF_JMP|BPF_JGE|BPF_K,0x40000000,0,1),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
    DENY_SYSCALL(socket), DENY_SYSCALL(socketpair), DENY_SYSCALL(connect),
    DENY_SYSCALL(bind), DENY_SYSCALL(listen), DENY_SYSCALL(accept), DENY_SYSCALL(accept4),
    DENY_SYSCALL(execve), DENY_SYSCALL(execveat), DENY_SYSCALL(fork), DENY_SYSCALL(vfork),
    DENY_SYSCALL(clone), DENY_SYSCALL(clone3), DENY_SYSCALL(ptrace),
    DENY_SYSCALL(process_vm_readv), DENY_SYSCALL(process_vm_writev),
    DENY_SYSCALL(pidfd_getfd), DENY_SYSCALL(kill), DENY_SYSCALL(tkill), DENY_SYSCALL(tgkill),
    DENY_SYSCALL(pidfd_send_signal), DENY_SYSCALL(rt_sigqueueinfo), DENY_SYSCALL(rt_tgsigqueueinfo),
    DENY_SYSCALL(prlimit64), DENY_SYSCALL(setpriority), DENY_SYSCALL(sched_setparam),
    DENY_SYSCALL(sched_setscheduler), DENY_SYSCALL(sched_setattr),
    DENY_SYSCALL(mount), DENY_SYSCALL(umount2), DENY_SYSCALL(unshare), DENY_SYSCALL(setns),
    DENY_SYSCALL(bpf), DENY_SYSCALL(userfaultfd), DENY_SYSCALL(perf_event_open),
    DENY_SYSCALL(io_uring_setup), DENY_SYSCALL(open_by_handle_at), DENY_SYSCALL(truncate),
    BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW)
  };
  struct sock_fprog program={sizeof(filter)/sizeof(filter[0]),filter};
  struct __user_cap_header_struct cap_header={_LINUX_CAPABILITY_VERSION_3,0};
  struct __user_cap_data_struct cap_data[2]={{0},{0}};
  if (!assets || !home) Arena_Fatal();
  if(syscall(SYS_capset,&cap_header,cap_data) ||
     prctl(PR_CAP_AMBIENT,PR_CAP_AMBIENT_CLEAR_ALL,0,0,0) ||
     prctl(PR_SET_DUMPABLE,0,0,0,0)) Arena_Fatal();
  abi=syscall(SYS_landlock_create_ruleset, NULL,0,LANDLOCK_CREATE_RULESET_VERSION);
  if (abi<1) Arena_Fatal();
  if(abi>=2) access|=LANDLOCK_ACCESS_FS_REFER;
  if(abi>=3) access|=LANDLOCK_ACCESS_FS_TRUNCATE;
  rules.handled_access_fs=access;
  ruleset=syscall(SYS_landlock_create_ruleset,&rules,sizeof(rules),0);
  if(ruleset<0) Arena_Fatal();
  Arena_PathRule(ruleset,assets,read_access);
  Arena_PathRule(ruleset,home,read_access|LANDLOCK_ACCESS_FS_WRITE_FILE|LANDLOCK_ACCESS_FS_REMOVE_DIR|LANDLOCK_ACCESS_FS_REMOVE_FILE|LANDLOCK_ACCESS_FS_MAKE_DIR|LANDLOCK_ACCESS_FS_MAKE_REG|(abi>=3?LANDLOCK_ACCESS_FS_TRUNCATE:0));
  Arena_PathRule(ruleset,"/dev/urandom",LANDLOCK_ACCESS_FS_READ_FILE);
  if(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0) || syscall(SYS_landlock_restrict_self,ruleset,0)) Arena_Fatal();
  close(ruleset);
  if(setrlimit(RLIMIT_AS,&memory) || setrlimit(RLIMIT_CORE,&core) || setrlimit(RLIMIT_NOFILE,&files) || setrlimit(RLIMIT_FSIZE,&size)) Arena_Fatal();
  if(prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&program)) Arena_Fatal();
}
#undef DENY_SYSCALL
