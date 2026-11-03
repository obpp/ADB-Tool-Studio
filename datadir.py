# -*- coding: utf-8 -*-
"""数据目录迁移：把 config/、scripts/、files/、logs/ 复制到新位置。

约定：

  * 只复制、不删除。迁移失败或者用户后悔了，旧目录还在。
  * 指针文件 data_dir.json 永远留在默认数据目录（见 apppaths），
    复制完最后一步才写；写之前任何一步失败都不切换。
  * 推送预设里存的是文件副本的绝对路径，复制完要改成新位置，
    否则删掉旧目录后这些预设就找不到文件了。

另有一条不复制路径 adopt()：目标目录里本来就有旧配置（备份、旧安装、
另一台机器拷来的），直接认它当数据目录，不碰里面的任何文件（只写指针）。
它适合「导入」，但预设里的绝对路径保持原样，不会像 migrate 那样改写。
"""

import json
import os
import shutil
import threading

import apppaths

# 跟着数据目录走的东西。WebView 缓存、临时上传目录不在其中：
# 前者必须在本机盘，后者在系统临时目录里，本来就不持久。
MIGRATE_DIRS = ("config", "scripts", "files", "logs")

_SKIP_PREFIX = ".part-"
_SKIP_SUFFIX = ".tmp"

_lock = threading.Lock()


class MigrateError(Exception):
    """迁移前的校验失败或目标冲突；message 直接给页面看。"""

    def __init__(self, message, conflicts=None, conflict_total=None):
        Exception.__init__(self, message)
        self.message = message
        self.conflicts = list(conflicts or [])
        self.conflict_total = conflict_total


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _inside(child, parent):
    """child 是否等于 parent 或在它里面；跨盘符返回 False。"""
    try:
        return os.path.commonpath([_norm(child), _norm(parent)]) == _norm(parent)
    except ValueError:
        return False


def _skip(name):
    return name.startswith(_SKIP_PREFIX) or name.endswith(_SKIP_SUFFIX)


def _dir_stats(root):
    files = total = 0
    if not os.path.isdir(root):
        return files, total
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if _skip(name):
                continue
            files += 1
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return files, total


def summarize(root):
    dirs = {}
    total_files = 0
    total_bytes = 0
    for name in MIGRATE_DIRS:
        files, size = _dir_stats(os.path.join(root, name))
        dirs[name] = {"files": files, "bytes": size}
        total_files += files
        total_bytes += size
    return {"dirs": dirs, "totalFiles": total_files, "totalBytes": total_bytes}


def info():
    """设置页要的全部信息：当前位置 + 指针状态 + 各目录文件统计。"""
    status = apppaths.data_dir_status()
    status.update(summarize(status["dataDir"]))
    status["logsDir"] = apppaths.logs_dir()
    return status


def validate(target, allow_nested=False):
    """把用户输入的目录变成绝对路径，并挡住明显会出事的几种选法。

    allow_nested 只给「恢复默认位置」用：自定义目录可能选在默认目录
    下面，也可能反过来把默认目录圈在里面，这时回默认必须放行，
    否则一次设置就再也回不去了。复制范围固定为四个子目录，不会递归。
    """
    raw = str(target or "").strip()
    if not raw:
        raise MigrateError("先填一个目录")
    target = os.path.abspath(os.path.expanduser(raw))
    current = apppaths.data_dir()

    if os.path.exists(target) and not os.path.isdir(target):
        raise MigrateError("这个路径是个文件，不是目录")
    if _norm(target) == _norm(current):
        raise MigrateError("新位置和当前数据目录是同一个")
    if not allow_nested and _inside(target, current):
        raise MigrateError("新位置不能在当前数据目录里面")
    if not allow_nested and _inside(current, target):
        raise MigrateError("新位置不能是当前数据目录的上级目录")
    return target


def inspect_existing(target):
    """检查一个「已经存在的数据目录」，只读，不改任何东西。

    跟 validate() 是两套规则：这里不复制，所以不限制目录嵌套 ——
    选的目录在默认目录里面、或者把默认目录圈在里面，都没有问题。
    """
    raw = str(target or "").strip()
    if not raw:
        raise MigrateError("先填一个目录")
    target = os.path.abspath(os.path.expanduser(raw))
    current = apppaths.data_dir()
    default = apppaths.default_data_dir()

    if not os.path.exists(target):
        raise MigrateError("这个目录不存在：%s" % target)
    if not os.path.isdir(target):
        raise MigrateError("这个路径是个文件，不是目录")
    if _norm(target) == _norm(current):
        raise MigrateError("这已经是当前数据目录")
    found = [name for name in MIGRATE_DIRS
             if os.path.isdir(os.path.join(target, name))]
    if not found:
        raise MigrateError("这个目录里没有 config/scripts/files/logs，不像是数据目录")
    if not apppaths._can_write(target):
        raise MigrateError("这个目录写不进去：%s" % target)

    result = {
        "dataDir": target,
        "defaultDir": default,
        "isCustom": _norm(target) != _norm(default),
        "logsDir": apppaths.logs_dir_for(target),
        "pointerPath": apppaths.data_dir_pointer_path(),
        "found": found,
    }
    result.update(summarize(target))
    return result


def _plan(src, dst):
    """列出要复制的文件和目标里已有的冲突，复制前先把冲突说清楚。"""
    copies = []
    conflicts = []
    for name in MIGRATE_DIRS:
        base = os.path.join(src, name)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for filename in filenames:
                if _skip(filename):
                    continue
                source = os.path.join(dirpath, filename)
                rel = os.path.relpath(source, src)
                target = os.path.join(dst, rel)
                if _norm(source) == _norm(target):
                    continue          # 恢复默认且目录嵌套时别自己拷自己
                if os.path.exists(target):
                    conflicts.append(rel)
                copies.append((source, target))
    return copies, conflicts


def _copy(copies):
    copied = 0
    total = 0
    for source, target in copies:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
        try:
            total += os.path.getsize(target)
        except OSError:
            pass
    return copied, total


def _rewrite_push_presets(old_root, new_root):
    """推送预设里的 files/push_file/ 绝对路径跟着换到新目录。"""
    path = os.path.join(new_root, "config", "push_file.presets.json")
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as fp:
            saved = json.load(fp)
    except (IOError, OSError, ValueError):
        return 0
    presets = saved.get("presets") if isinstance(saved, dict) else None
    if not isinstance(presets, list):
        return 0

    old_files = os.path.join(old_root, "files")
    new_files = os.path.join(new_root, "files")
    changed = 0
    for preset in presets:
        if not isinstance(preset, dict):
            continue
        files = preset.get("files")
        if not isinstance(files, list):
            continue
        for item in files:
            if not isinstance(item, dict):
                continue
            current = item.get("path")
            if not isinstance(current, str) or not _inside(current, old_files):
                continue
            rel = os.path.relpath(os.path.abspath(current),
                                  os.path.abspath(old_files))
            item["path"] = os.path.normpath(os.path.join(new_files, rel))
            changed += 1

    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(saved, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return changed


def migrate(target, overwrite=False, allow_nested=False):
    """把当前数据目录复制到 target，成功后写指针并切换。

    返回统计信息；冲突在 overwrite 为假时抛 MigrateError（带 conflicts）。
    """
    if not _lock.acquire(blocking=False):
        raise MigrateError("另一个迁移正在进行，等它结束再试")
    try:
        # 拿到锁之后再校验：两个标签页同时点的时候，当前目录可能刚被
        # 另一个迁移换掉，必须按最新的位置来判断
        target = validate(target, allow_nested)
        current = apppaths.data_dir()
        default = apppaths.default_data_dir()

        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            raise MigrateError("建不了新目录：%s" % exc)
        if not apppaths._can_write(target):
            raise MigrateError("新目录写不进去：%s" % target)

        copies, conflicts = _plan(current, target)
        if conflicts and not overwrite:
            raise MigrateError(
                "目标目录里已有 %d 个同名文件" % len(conflicts),
                conflicts[:20], len(conflicts))

        copied_files, copied_bytes = _copy(copies)
        rewritten = _rewrite_push_presets(current, target)

        # 复制全部成功后才写指针。指针写失败时旧目录还在用，
        # 新目录里只是多了一份副本，不会丢数据。
        if _norm(target) == _norm(default):
            apppaths.clear_data_dir_pointer()
        else:
            apppaths.write_data_dir_pointer(target)
        apppaths.apply_data_dir(target)

        result = {
            "copied": {"files": copied_files, "bytes": copied_bytes},
            "rewritten": rewritten,
            "dataDir": target,
            "defaultDir": default,
            "isCustom": _norm(target) != _norm(default),
            "logsDir": apppaths.logs_dir(),
            "pointerPath": apppaths.data_dir_pointer_path(),
        }
        result.update(summarize(target))
        return result
    finally:
        _lock.release()


def adopt(target):
    """把一个已有目录直接设为数据目录：不复制、不修改它的内容。

    校验和锁的用法跟 migrate 一致：拿到锁之后再检查，避免两个标签页
    同时点的时候按过期的当前目录判断。写指针是唯一改动。
    """
    if not _lock.acquire(blocking=False):
        raise MigrateError("另一个迁移正在进行，等它结束再试")
    try:
        result = inspect_existing(target)
        target = result["dataDir"]
        if _norm(target) == _norm(result["defaultDir"]):
            apppaths.clear_data_dir_pointer()
        else:
            apppaths.write_data_dir_pointer(target)
        apppaths.apply_data_dir(target)

        result["adopted"] = True
        result["copied"] = {"files": 0, "bytes": 0}
        result["rewritten"] = 0
        result["logsDir"] = apppaths.logs_dir()
        return result
    finally:
        _lock.release()


def reset(overwrite=False):
    """回到默认数据目录。

    正常情况下就是 migrate(default)。还有一种特殊情况：指针指向一个
    已经不存在的目录时，apppaths 启动时就回退到了默认目录 —— 这时
    实际位置已经是默认目录，但坏指针还在，每次启动都会报一句回退原因。
    这种情况下只清指针，不复制。
    """
    target = apppaths.default_data_dir()
    if not _lock.acquire(blocking=False):
        raise MigrateError("另一个迁移正在进行，等它结束再试")
    try:
        if _norm(apppaths.data_dir()) == _norm(target):
            apppaths.clear_data_dir_pointer()
            apppaths.apply_data_dir(target)
            result = {
                "copied": {"files": 0, "bytes": 0},
                "rewritten": 0,
                "dataDir": target,
                "defaultDir": target,
                "isCustom": False,
                "logsDir": apppaths.logs_dir(),
                "pointerPath": apppaths.data_dir_pointer_path(),
            }
            result.update(summarize(target))
            return result
    finally:
        _lock.release()
    return migrate(target, overwrite, allow_nested=True)
