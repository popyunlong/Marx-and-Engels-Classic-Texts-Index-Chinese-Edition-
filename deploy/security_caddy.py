"""Incremental patch of the actual Caddyfile, called only by a locked release.

Refuse unknown site/log layouts. Never replace headers, named AI upstreams,
origin-lock imports or payment routes with a repository sample.
"""
from pathlib import Path
import re
import sys

BEGIN = "\t# BEGIN reader-security-v1\n"
END = "\t# END reader-security-v1\n"
BLOCK = BEGIN + r'''	@reader_sensitive path_regexp reader_sensitive (?i)(^|/)(\.env|\.git|service_account\.json|gc-service\.json|gcp-service\.json)(/|$)
	respond @reader_sensitive 403
''' + END
LOG_BEGIN = "\t# BEGIN reader-access-log-v1\n"
LOG_END = "\t# END reader-access-log-v1\n"
LOG_BLOCK = LOG_BEGIN + '''\tlog {
\t\toutput file /var/log/caddy/access.log {
\t\t\troll_size 25MiB
\t\t\troll_keep 8
\t\t\troll_keep_for 720h
\t\t}
\t}
''' + LOG_END


def patch(source, remove=False):
    if BEGIN in source:
        start = source.index(BEGIN)
        end = source.index(END, start) + len(END)
        source = source[:start] + source[end:]
    if LOG_BEGIN in source:
        start = source.index(LOG_BEGIN)
        end = source.index(LOG_END, start) + len(LOG_END)
        source = source[:start] + "\tlog\n" + source[end:]
    if remove:
        return source
    matches = list(re.finditer(r"(?m)^mazhuzuojiansuo\.com\s*\{\s*\n", source))
    if len(matches) != 1:
        raise ValueError("actual site block requires review; refusing a sample overwrite")
    pos = matches[0].end()
    source = source[:pos] + BLOCK + source[pos:]
    source, count = re.subn(r"(?m)^[ \t]*log[ \t]*$", lambda _: LOG_BLOCK.rstrip(), source)
    if count != 1:
        raise ValueError("actual logging configuration requires review")
    return source


def prepare_access_log(directory=Path("/var/log/caddy"), uid=None, gid=None):
    """Preserve existing evidence; never follow links or truncate a log."""
    import os
    import stat
    if uid is None or gid is None:
        import pwd
        account = pwd.getpwnam("caddy")
        uid, gid = account.pw_uid, account.pw_gid
    directory = Path(directory)
    directory.mkdir(mode=0o750, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(directory_fd).st_uid not in (0, uid):
            raise ValueError("unexpected access-log directory owner")
        os.fchown(directory_fd, uid, gid)
        os.fchmod(directory_fd, 0o750)
        log_fd = os.open("access.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                         os.O_NOFOLLOW | os.O_NONBLOCK, 0o640, dir_fd=directory_fd)
        try:
            info = os.fstat(log_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("access log must be a regular file with one link")
            if info.st_uid not in (0, uid):
                raise ValueError("unexpected access-log owner")
            os.fchown(log_fd, uid, gid)
            os.fchmod(log_fd, 0o640)
        finally:
            os.close(log_fd)
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare-log"]:
        prepare_access_log()
    else:
        source, target = map(Path, sys.argv[1:3])
        target.write_text(patch(source.read_text(encoding="utf-8"), remove="--remove" in sys.argv[3:]), encoding="utf-8")
