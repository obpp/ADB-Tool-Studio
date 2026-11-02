/* 推送文件 二级页面 */
(function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var DEVICE_STATE = {
    'device': '已连接', 'unauthorized': '未授权', 'offline': '离线',
    'no permissions': '无权限', 'recovery': 'recovery',
    'sideload': 'sideload', 'bootloader': 'fastboot'
  };

  var client = new ADB.ToolClient('push_file');
  var log = new ADB.LogConsole($('#log'), $('#follow'));
  var dirInput = $('#cfg-pushdir');

  var state = null;
  var files = [];
  var devices = [];
  var cfgDirty = false;
  var uploadBusy = false;
  var presetUploadBusy = false;   // 正在把选中的文件复制进数据目录
  var renderKey = '';        // 清单没变就不重画

  var presets = [];
  var selectedId = '';
  var runningId = '';
  var editingId = '';
  var presetFiles = [];      // 表单里正在编辑的文件列表（只记路径）
  var presetDirty = false;
  var presetRenderKey = '';
  var lastBrowseDir = '';

  // 推送内容的两种来源。页头那个主按钮推的就是当前标签里的东西。
  var mode = 'temp';
  var MODE_KEY = 'ats-push-mode';
  try {
    var savedMode = window.localStorage && window.localStorage.getItem(MODE_KEY);
    if (savedMode === 'temp' || savedMode === 'preset') mode = savedMode;
  } catch (err) { /* 隐私模式下没有 localStorage，照常用默认值 */ }

  client.onState(renderState);
  client.onLog(function (entry) { log.append(entry); });

  client.connect(function (snap) {
    applyConfig(snap.config, true);
    log.clear();
    log.appendAll(snap.history);
  });

  /* ---------------- 状态渲染 ---------------- */

  function renderState(s) {
    state = s;
    var stats = s.stats || {};
    var extra = s.extra || {};

    files = extra.files || files;
    // 后端的长驻监听会在设备上线时推 state，这里跟着更新
    devices = extra.devices || devices;
    presets = extra.presets || presets;
    selectedId = extra.selected || '';
    runningId = extra.runningId || '';
    lastBrowseDir = extra.lastBrowseDir || lastBrowseDir;

    var override = null;
    if (s.status === 'idle' && stats.lastExitCode) {
      override = { tone: 'error', icon: '⚠', text: '上次推送失败' };
    }
    ADB.paintBadge($('#badge'), s.status, override);

    $('#kpi-pushed').textContent = ADB.num(stats.filesPushed);
    $('#kpi-failed').textContent = ADB.num(stats.filesFailed);
    $('#meta-exit').textContent = (stats.lastExitCode === null ||
      stats.lastExitCode === undefined) ? '—' : stats.lastExitCode;
    $('#meta-elapsed').textContent = stats.lastDuration
      ? stats.lastDuration.toFixed(1) + ' 秒' : '—';

    updateTargetLabel();
    fillSerialOptions();
    renderFiles();
    updatePresetForm();
    renderPresets();
    updateReadouts();
    syncButtons();
  }

  // 页头的「待推送 / 推送目录」跟着标签走：临时文件看上传队列，
  // 预设看选中的那一个。
  function updateReadouts() {
    if (mode === 'preset') {
      var preset = findPreset(selectedId);
      $('#meta-count').textContent = preset ? (preset.files || []).length : 0;
      $('#meta-dir').textContent = (preset && preset.dir) || '—';
      return;
    }
    $('#meta-count').textContent = files.length;
    var cfg = state && state.config;
    $('#meta-dir').textContent = (cfg && cfg.pushDir) || '—';
  }

  function syncButtons() {
    var running = !!state && state.status !== 'idle';
    var busy = running || uploadBusy || presetUploadBusy;
    // 临时文件看队列，预设看有没有选中
    var canRun = mode === 'preset'
      ? !!findPreset(selectedId)
      : !!files.length;

    $('#btn-run').disabled = busy || !canRun;
    $('#btn-stop').disabled = !running;
    $('#btn-add').disabled = busy;
    $('#btn-clear-files').disabled = busy || !files.length;

    $('#btn-new-preset').disabled = running;
    $('#btn-add-preset-file').disabled = running || presetUploadBusy;
    $('#btn-delete-preset').disabled = running || !editingId;

    var buttons = $('#file-list').querySelectorAll('button');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = busy;
    }

    var rows = $('#preset-files').querySelectorAll('.preset-file-remove');
    for (var j = 0; j < rows.length; j++) {
      rows[j].disabled = running;
    }
  }

  function updateTargetLabel() {
    var el = $('#meta-target');
    var cfg = state && state.config;
    if (!cfg) { el.textContent = '—'; return; }
    if (cfg.serial) { el.textContent = cfg.serial; return; }
    var online = devices.filter(function (d) { return d.state === 'device'; });
    if (online.length === 1) el.textContent = online[0].serial + '（自动）';
    else if (online.length === 0) el.textContent = '无在线设备';
    else el.textContent = online.length + ' 台在线，未选择';
  }

  setInterval(function () {
    var el = $('#meta-uptime');
    if (!state || !state.startedAt || state.status === 'idle') { el.textContent = '—'; return; }
    el.textContent = ADB.duration(Date.now() / 1000 - state.startedAt);
  }, 1000);

  /* ---------------- 文件列表 ---------------- */

  function sizeText(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
    return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
  }

  function renderFiles() {
    var list = $('#file-list');
    if (!list) return;

    var key = files.map(function (f) {
      return [f.id, f.name, f.size].join('|');
    }).join('\n');
    if (key === renderKey) return;
    renderKey = key;

    list.textContent = '';

    if (!files.length) {
      var empty = document.createElement('li');
      empty.className = 'dev-empty';
      empty.textContent = '还没有文件。把文件拖进这里，或点「选择文件…」。';
      list.appendChild(empty);
      return;
    }

    files.forEach(function (item) {
      list.appendChild(buildFileRow(item));
    });
  }

  function buildFileRow(item) {
    var li = document.createElement('li');
    li.className = 'script';

    var name = document.createElement('span');
    name.className = 'script-name';
    name.textContent = item.name;
    name.title = item.name;

    var meta = document.createElement('span');
    meta.className = 'script-meta';
    meta.textContent = sizeText(item.size);

    var ops = document.createElement('span');
    ops.className = 'script-ops';

    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn ghost small';
    del.textContent = '移除';
    del.addEventListener('click', function () {
      del.disabled = true;
      client.post('files/remove', { id: item.id }).then(function (data) {
        if (!data.ok) {
          setContentStatus(data.error || '移除失败', true);
          del.disabled = false;
          return;
        }
        files = data.files || [];
        renderKey = '';
        renderFiles();
        syncButtons();
      }).catch(function () {
        setContentStatus('请求失败，后端还在吗？', true);
        del.disabled = false;
      });
    });
    ops.appendChild(del);

    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(ops);
    return li;
  }

  /* ---------------- 上传 ---------------- */

  // 内容区的提示只有一处，两个标签共用
  function setContentStatus(text, isError) {
    var el = $('#content-status');
    if (!el) return;
    if (isError) el.setAttribute('data-tone', 'error');
    else el.removeAttribute('data-tone');
    el.textContent = text || '';
  }

  function uploadFile(file) {
    return fetch('/api/push_file/files/upload?name=' + encodeURIComponent(file.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: 'HTTP ' + r.status };
      });
    }).then(function (data) {
      if (!data.ok) throw new Error(data.error || '上传失败');
      return data;
    });
  }

  function addFiles(fileList) {
    var pending = Array.prototype.slice.call(fileList || []);
    if (!pending.length || uploadBusy) return;
    if (state && state.status !== 'idle') {
      setContentStatus('正在推送，等这次结束再加文件', true);
      return;
    }

    uploadBusy = true;
    syncButtons();
    setContentStatus('正在上传 0/' + pending.length + '…');

    var done = 0;
    var errors = [];

    // 一个一个传：同时开多个大文件上传，进度和错误都看不清
    var chain = Promise.resolve();
    pending.forEach(function (file) {
      chain = chain.then(function () {
        return uploadFile(file).then(function (data) {
          if (data.files) files = data.files;
        }).catch(function (err) {
          errors.push((file.name || '文件') + '：' +
            ((err && err.message) || '上传失败'));
        }).then(function () {
          done++;
          renderKey = '';
          renderFiles();
          syncButtons();
          setContentStatus('正在上传 ' + done + '/' + pending.length + '…');
        });
      });
    });

    chain.then(function () {
      if (errors.length) {
        setContentStatus('有 ' + errors.length + ' 个文件没传上去：' +
          errors[0] + (errors.length > 1 ? ' 等' : ''), true);
        log.append({ time: clock(), level: 'err',
          msg: '上传失败：' + errors.join('；') });
      } else {
        setContentStatus('已加入 ' + done + ' 个文件');
      }
    }).catch(function () {
      setContentStatus('上传中断', true);
    }).then(function () {
      uploadBusy = false;
      syncButtons();
    });
  }

  function clock() {
    var d = new Date();
    return ADB.pad(d.getHours()) + ':' + ADB.pad(d.getMinutes()) + ':' +
      ADB.pad(d.getSeconds());
  }

  $('#btn-add').addEventListener('click', function () { $('#file-input').click(); });

  $('#file-input').addEventListener('change', function (ev) {
    var picked = ev.target.files ? Array.prototype.slice.call(ev.target.files) : [];
    ev.target.value = '';           // 允许重复选同一个文件
    addFiles(picked);
  });

  $('#btn-clear-files').addEventListener('click', function () {
    var btn = $('#btn-clear-files');
    btn.disabled = true;
    client.post('files/clear', {}).then(function (data) {
      if (!data.ok) {
        setContentStatus(data.error || '清空失败', true);
        return;
      }
      files = [];
      renderKey = '';
      renderFiles();
    }).catch(function () {
      setContentStatus('请求失败，后端还在吗？', true);
    }).then(function () {
      syncButtons();
    });
  });

  // 拖放范围就是这个虚线框。dragenter / dragleave 会从子元素冒泡上来，
  // 用计数避免划过每一行时闪烁。
  var zone = $('#push-drop');
  var dragDepth = 0;
  zone.addEventListener('dragenter', function (ev) {
    ev.preventDefault();
    dragDepth++;
    zone.classList.add('is-drop');
  });
  zone.addEventListener('dragover', function (ev) { ev.preventDefault(); });
  zone.addEventListener('dragleave', function (ev) {
    ev.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) zone.classList.remove('is-drop');
  });
  zone.addEventListener('drop', function (ev) {
    ev.preventDefault();
    dragDepth = 0;
    zone.classList.remove('is-drop');
    if (uploadBusy) { setContentStatus('还在上传上一批文件，稍等', true); return; }
    addFiles(ev.dataTransfer && ev.dataTransfer.files);
  });

  /* ---------------- 预设 ---------------- */

  function findPreset(id) {
    for (var i = 0; i < presets.length; i++) {
      if (presets[i].id === id) return presets[i];
    }
    return null;
  }

  function baseName(path) {
    if (!path) return '';
    var parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || path;
  }

  function dirName(path) {
    if (!path) return '';
    var text = String(path).replace(/[\\/]+$/, '');
    var cut = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'));
    if (cut < 0) return '';
    return text.slice(0, cut) || '/';
  }

  function dateText(epochSeconds) {
    if (!epochSeconds) return '';
    var d = new Date(epochSeconds * 1000);
    return (d.getMonth() + 1) + '-' + d.getDate() + ' ' +
      ADB.pad(d.getHours()) + ':' + ADB.pad(d.getMinutes());
  }

  function setMode(next, silent) {
    if (next !== 'temp' && next !== 'preset') return;
    mode = next;
    try {
      window.localStorage.setItem(MODE_KEY, next);
    } catch (err) { /* 存不了就只在这次会话里生效 */ }

    var tempTab = $('#mode-temp');
    var presetTab = $('#mode-preset');
    if (mode === 'temp') {
      tempTab.classList.add('is-active');
      presetTab.classList.remove('is-active');
    } else {
      presetTab.classList.add('is-active');
      tempTab.classList.remove('is-active');
    }
    tempTab.setAttribute('aria-selected', mode === 'temp' ? 'true' : 'false');
    presetTab.setAttribute('aria-selected', mode === 'preset' ? 'true' : 'false');
    $('#tab-temp').hidden = mode !== 'temp';
    $('#tab-preset').hidden = mode !== 'preset';

    if (!silent) setContentStatus('');
    updateReadouts();
    syncButtons();
  }

  $('#mode-temp').addEventListener('click', function () { setMode('temp'); });
  $('#mode-preset').addEventListener('click', function () { setMode('preset'); });

  function renderPresets() {
    var list = $('#preset-list');
    if (!list) return;

    var key = presets.map(function (p) {
      return [p.id, p.name, JSON.stringify(p.files), p.dir,
        p.lastRunAt, p.lastExitCode].join('|');
    }).join('\n') + '#' + selectedId + '#' + runningId;
    if (key === presetRenderKey) return;
    presetRenderKey = key;

    list.textContent = '';
    // 收起时也能从标题行看出有几个
    var counter = $('#preset-count');
    if (counter) counter.textContent = presets.length ? presets.length + ' 个' : '';

    if (!presets.length) {
      var empty = document.createElement('li');
      empty.className = 'dev-empty';
      empty.textContent = '还没有预设，在下面填好文件和目录后点「保存预设」';
      list.appendChild(empty);
      return;
    }

    presets.forEach(function (item) {
      list.appendChild(buildPresetRow(item));
    });
  }

  function buildPresetRow(item) {
    var li = document.createElement('li');
    li.className = 'script';
    li.tabIndex = 0;
    li.setAttribute('role', 'button');
    if (item.id === selectedId) {
      li.classList.add('is-selected');
      li.setAttribute('aria-pressed', 'true');
    } else {
      li.setAttribute('aria-pressed', 'false');
    }
    if (item.id === runningId) li.classList.add('is-running');

    var name = document.createElement('span');
    name.className = 'script-name';
    name.textContent = item.name;

    var meta = document.createElement('span');
    meta.className = 'script-meta';
    var count = (item.files || []).length;
    var bits = [count + ' 个文件', '→', item.dir || '未设目录'];
    if (item.lastRunAt) {
      bits.push('上次 ' + dateText(item.lastRunAt) +
        (item.lastExitCode === null || item.lastExitCode === undefined
          ? ' 异常' : ' 退出码 ' + item.lastExitCode));
    }
    meta.textContent = bits.join(' ');

    li.appendChild(name);
    li.appendChild(meta);

    var detail = (item.files || []).map(function (f) {
      return '  ' + f.name + '  (' + f.path + ')';
    }).join('\n');
    li.title = item.name + '\n' + detail +
      '\n推到 ' + (item.dir || '未设目录') + '\n双击直接推送';

    li.addEventListener('click', function () { selectPreset(item.id); });
    // 双击 = 选中后直接按这个预设推，和页头「开始推送」一样。
    // 正在编辑同一个预设时别让选中把它冲掉，先保存再双击。
    li.addEventListener('dblclick', function () {
      if (presetDirty && editingId === item.id) {
        setContentStatus('预设表单有未保存的修改，先保存再推送', true);
        return;
      }
      selectPreset(item.id);
      runPreset();
    });
    li.addEventListener('keydown', function (ev) {
      if (ev.target !== li) return;
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        selectPreset(item.id);
      }
    });
    return li;
  }

  function selectPreset(id) {
    var preset = findPreset(id);
    if (!preset) return;
    fillPresetForm(preset);
    presetDirty = false;
    setContentStatus('');
    if (id !== selectedId) {
      selectedId = id;
      client.post('config', { selected: id });
    }
    presetRenderKey = '';
    renderPresets();
    updateReadouts();
    syncButtons();
  }

  // 第一次打开页面（或别的窗口换了选择）时，把选中的预设铺到表单里。
  // 表单有未保存的修改就别动它。
  function updatePresetForm() {
    if (presetDirty) return;
    if (selectedId && editingId !== selectedId) {
      var fill = findPreset(selectedId);
      if (fill) fillPresetForm(fill);
      return;
    }
    if (!selectedId && editingId && !findPreset(editingId)) {
      fillPresetForm(blankPreset());
    }
  }

  /* ---------------- 预设表单 ---------------- */

  function blankPreset() {
    return { id: '', name: '', dir: '', files: [] };
  }

  function fillPresetForm(preset) {
    editingId = preset.id || '';
    $('#preset-name').value = preset.name || '';
    $('#preset-dir').value = preset.dir || '';
    presetFiles = (preset.files || []).map(function (f) {
      return { path: f.path, name: f.name || baseName(f.path) };
    });
    renderPresetFiles();
    syncButtons();
  }

  function renderPresetFiles() {
    var list = $('#preset-files');
    if (!list) return;
    list.textContent = '';

    if (!presetFiles.length) {
      var empty = document.createElement('li');
      empty.className = 'dev-empty';
      empty.textContent = '还没有文件。把文件拖进来，或点下面的「选择文件…」。';
      list.appendChild(empty);
      return;
    }

    presetFiles.forEach(function (item, index) {
      list.appendChild(buildPresetFileRow(item, index));
    });
    syncButtons();
  }

  function buildPresetFileRow(item, index) {
    var li = document.createElement('li');
    li.className = 'preset-file';

    var name = document.createElement('span');
    name.className = 'preset-file-name';
    name.textContent = item.name || baseName(item.path);
    name.title = item.path;

    var path = document.createElement('span');
    path.className = 'preset-file-path';
    path.textContent = item.path;
    path.title = item.path;

    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn ghost small preset-file-remove';
    del.textContent = '移除';
    del.addEventListener('click', function () {
      presetFiles.splice(index, 1);
      renderPresetFiles();
      markPresetDirty();
    });

    li.appendChild(name);
    li.appendChild(path);
    li.appendChild(del);
    return li;
  }

  function markPresetDirty() {
    presetDirty = true;
    setContentStatus('有未保存的修改');
  }

  function addPresetPaths(paths, silent) {
    var added = 0;
    (paths || []).forEach(function (raw) {
      var path = String(raw || '').trim();
      if (!path) return;
      var exists = presetFiles.some(function (f) { return f.path === path; });
      if (exists) return;
      presetFiles.push({ path: path, name: baseName(path) });
      added++;
    });
    if (!added) {
      if (!silent) setContentStatus('这些文件已经在预设里了');
      return;
    }
    renderPresetFiles();
    markPresetDirty();
    if (!silent) setContentStatus('已加入 ' + added + ' 个文件，记得保存预设');
  }

  // 桌面版有原生桥，能直接弹系统的多选文件对话框并拿到完整路径；
  // 网页版没有 —— 浏览器不会把完整路径交给页面，只能把文件复制一份进来。
  function nativeFilePicker() {
    var api = window.pywebview && window.pywebview.api;
    return api && typeof api.pick_files === 'function' ? api : null;
  }

  function startBrowseDir() {
    if (presetFiles.length) {
      return dirName(presetFiles[presetFiles.length - 1].path);
    }
    return lastBrowseDir || '';
  }

  function uploadPresetFile(file) {
    return fetch('/api/push_file/presets/upload?name=' +
                 encodeURIComponent(file.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: 'HTTP ' + r.status };
      });
    }).then(function (data) {
      if (!data.ok) throw new Error(data.error || '复制失败');
      return data;
    });
  }

  // 拖进来、或者网页版选的文件：浏览器只给 File 对象，没有本机路径，
  // 所以先复制一份到后端的数据目录，再把那份的路径记进预设。
  function addPresetFileList(fileList) {
    var pending = Array.prototype.slice.call(fileList || []);
    if (!pending.length || presetUploadBusy) return;
    if (state && state.status !== 'idle') {
      setContentStatus('正在推送，等这次结束再加文件', true);
      return;
    }

    presetUploadBusy = true;
    syncButtons();
    setContentStatus('正在复制 0/' + pending.length + '…');

    var done = 0;
    var errors = [];

    // 一个一个来，和临时文件上传一样：同时开多个大文件看不清卡在哪
    var chain = Promise.resolve();
    pending.forEach(function (file) {
      chain = chain.then(function () {
        return uploadPresetFile(file).then(function (data) {
          addPresetPaths([data.path], true);
        }).catch(function (err) {
          errors.push((file.name || '文件') + '：' +
            ((err && err.message) || '复制失败'));
        }).then(function () {
          done++;
          setContentStatus('正在复制 ' + done + '/' + pending.length + '…');
        });
      });
    });

    chain.then(function () {
      if (errors.length) {
        setContentStatus('有 ' + errors.length + ' 个文件没复制进来：' +
          errors[0] + (errors.length > 1 ? ' 等' : ''), true);
        log.append({ time: clock(), level: 'err',
          msg: '复制到预设失败：' + errors.join('；') });
      } else {
        setContentStatus('已加入 ' + done + ' 个文件，记得保存预设');
      }
    }).catch(function () {
      setContentStatus('复制中断', true);
    }).then(function () {
      presetUploadBusy = false;
      syncButtons();
    });
  }

  $('#btn-add-preset-file').addEventListener('click', function () {
    var api = nativeFilePicker();
    if (!api) { $('#preset-file-input').click(); return; }
    api.pick_files(startBrowseDir()).then(function (paths) {
      addPresetPaths(paths || []);
    }).catch(function () { $('#preset-file-input').click(); });
  });

  $('#preset-file-input').addEventListener('change', function (ev) {
    var picked = ev.target.files ? Array.prototype.slice.call(ev.target.files) : [];
    ev.target.value = '';           // 允许重复选同一个文件
    addPresetFileList(picked);
  });

  // 拖放范围和临时文件那个虚线框一样，只是加进来的是文件路径（复制一份）
  var presetZone = $('#preset-drop');
  var presetDragDepth = 0;
  presetZone.addEventListener('dragenter', function (ev) {
    ev.preventDefault();
    presetDragDepth++;
    presetZone.classList.add('is-drop');
  });
  presetZone.addEventListener('dragover', function (ev) { ev.preventDefault(); });
  presetZone.addEventListener('dragleave', function (ev) {
    ev.preventDefault();
    presetDragDepth = Math.max(0, presetDragDepth - 1);
    if (!presetDragDepth) presetZone.classList.remove('is-drop');
  });
  presetZone.addEventListener('drop', function (ev) {
    ev.preventDefault();
    presetDragDepth = 0;
    presetZone.classList.remove('is-drop');
    addPresetFileList(ev.dataTransfer && ev.dataTransfer.files);
  });

  var presetForm = $('#preset-form');

  presetForm.addEventListener('input', markPresetDirty);
  presetForm.addEventListener('change', markPresetDirty);
  presetForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    savePreset();
  });

  function savePreset() {
    var dir = $('#preset-dir').value.trim();
    if (!dir || dir.charAt(0) !== '/') {
      setContentStatus('设备上的目录必须是 / 开头的绝对路径', true);
      $('#preset-dir').focus();
      return;
    }
    if (dir === '/') {
      setContentStatus('不能推到根目录，填一个具体的目录', true);
      $('#preset-dir').focus();
      return;
    }
    if (!presetFiles.length) {
      setContentStatus('先添加至少 1 个本地文件', true);
      $('#btn-add-preset-file').focus();
      return;
    }

    var payload = {
      id: editingId,
      name: $('#preset-name').value.trim(),
      dir: dir,
      files: presetFiles.map(function (f) { return { path: f.path }; })
    };

    var btn = presetForm.querySelector('button[type="submit"]');
    btn.disabled = true;
    setContentStatus('保存中…');

    client.post('presets/save', payload).then(function (data) {
      btn.disabled = false;
      if (!data || !data.ok) {
        setContentStatus((data && data.error) || '保存失败', true);
        return;
      }
      presets = data.presets || presets;
      selectedId = data.preset.id;
      presetDirty = false;
      fillPresetForm(data.preset);
      presetRenderKey = '';
      renderPresets();
      syncButtons();

      var text = data.saved ? '已保存' : '已生效，但写入配置文件失败';
      if (data.missing && data.missing.length) {
        text += ' · 有 ' + data.missing.length + ' 个文件现在找不到，推送时会跳过';
      }
      setContentStatus(text, !data.saved);
    }).catch(function () {
      btn.disabled = false;
      setContentStatus('请求失败，后端还在吗？', true);
    });
  }

  $('#btn-new-preset').addEventListener('click', function () {
    editingId = '';
    selectedId = '';
    fillPresetForm(blankPreset());
    presetDirty = false;
    presetRenderKey = '';
    renderPresets();
    updateReadouts();
    syncButtons();
    setContentStatus('');
    client.post('config', { selected: '' });
    $('#preset-name').focus();
  });

  $('#btn-delete-preset').addEventListener('click', function () {
    if (!editingId) return;
    var btn = $('#btn-delete-preset');

    if (btn.getAttribute('data-armed') !== '1') {
      // 第一次点只是「上膛」，3 秒内再点才真的删
      btn.setAttribute('data-armed', '1');
      btn.textContent = '确认删除';
      setTimeout(function () {
        if (btn.getAttribute('data-armed') !== '1') return;
        btn.removeAttribute('data-armed');
        btn.textContent = '删除';
      }, 3000);
      return;
    }

    var deletedId = editingId;
    btn.removeAttribute('data-armed');
    btn.textContent = '删除';
    btn.disabled = true;

    client.post('presets/delete', { id: deletedId }).then(function (data) {
      btn.disabled = false;
      if (!data || !data.ok) {
        setContentStatus((data && data.error) || '删除失败', true);
        return;
      }
      presets = data.presets || [];
      selectedId = data.selected || '';
      presetDirty = false;
      fillPresetForm((selectedId && findPreset(selectedId)) || blankPreset());
      presetRenderKey = '';
      renderPresets();
      updateReadouts();
      syncButtons();
      setContentStatus('已删除');
    }).catch(function () {
      btn.disabled = false;
      setContentStatus('请求失败，后端还在吗？', true);
    });
  });

  // 预设标签下的主按钮行为：直接按预设推，不经过上传队列。
  // 页头按钮和列表双击都走这里。
  function runPreset() {
    if (state && state.status !== 'idle') {
      setContentStatus('正在推送，先停止再操作', true);
      return;
    }
    if (!selectedId) {
      setContentStatus('先在列表里选一个预设', true);
      return;
    }
    var preset = findPreset(selectedId);
    if (!preset) {
      setContentStatus('这个预设已经不在了，刷新一下', true);
      return;
    }
    if (presetDirty) {
      setContentStatus('预设表单有未保存的修改，先保存再推送', true);
      return;
    }

    var btn = $('#btn-run');
    btn.disabled = true;
    // 先把 adb 路径 / 设备 / remount 落盘，再按预设启动：
    // 预设自己的目录和文件在启动时读取，不走虚线框里的上传列表
    client.saveConfig(collectConfig()).then(function (data) {
      if (data && data.ok === false) {
        setContentStatus(
          (data && (data.error || data.message)) || '保存参数失败', true);
        return null;
      }
      if (data && data.config) applyConfig(data.config, true);
      return client.start({ presetId: selectedId });
    }).then(function (data) {
      if (data && !data.ok) {
        setContentStatus(data.message || '启动失败', true);
      }
    }).catch(function () {
      setContentStatus('请求失败，后端还在吗？', true);
    }).then(function () {
      syncButtons();
    });
  }

  /* ---------------- 设备目录浏览器 ---------------- */

  var picker = { path: '/', parent: '' };

  function joinPath(base, name) {
    if (!base || base === '/') return '/' + name;
    return base.replace(/\/+$/, '') + '/' + name;
  }

  // 读目录失败时服务端不给 parent，自己算一个，免得卡在错的路径里出不来
  function parentDir(path) {
    if (!path || path === '/') return '';
    return path.replace(/\/+$/, '').replace(/\/[^/]*$/, '') || '/';
  }

  function openPicker() {
    var start = dirInput.value.trim();
    var fallback = state && state.config && state.config.pushDir;
    if (start && start.charAt(0) !== '/') start = '';
    if (!start) start = fallback || '/';
    picker.path = start;
    $('#picker').hidden = false;
    loadDir(start);
    $('#picker-close').focus();
  }

  function closePicker() {
    $('#picker').hidden = true;
    dirInput.focus();
  }

  function loadDir(path) {
    $('#picker-error').textContent = '正在读取…';
    $('#picker-list').textContent = '';
    $('#picker-use').disabled = true;

    client.post('fs/list', { path: path || '/' }).then(function (data) {
      if (!data || !data.ok) {
        $('#picker-error').textContent = (data && data.error) || '读不了这个目录';
        picker.path = path || '/';
        picker.parent = parentDir(picker.path);
        $('#picker-path').value = picker.path;
        $('#picker-up').disabled = !picker.parent;
        return;
      }
      picker.path = data.path;
      picker.parent = data.parent || '';
      $('#picker-path').value = data.path;
      $('#picker-error').textContent = data.truncated
        ? '目录太多，只显示了前 500 个' : '';
      $('#picker-use').disabled = false;
      $('#picker-up').disabled = !picker.parent;
      renderPicker(data);
    }).catch(function () {
      $('#picker-error').textContent = '请求失败，后端还在吗？';
    });
  }

  function renderPicker(data) {
    var list = $('#picker-list');
    list.textContent = '';

    if (data.parent) list.appendChild(pickerRow('..', data.parent));
    (data.dirs || []).forEach(function (name) {
      list.appendChild(pickerRow(name, joinPath(data.path, name)));
    });

    if (!data.parent && !(data.dirs || []).length) {
      var empty = document.createElement('li');
      empty.className = 'picker-empty';
      empty.textContent = '这个目录下没有子目录（也可能没有权限读）';
      list.appendChild(empty);
    }
  }

  function pickerRow(name, path) {
    var li = document.createElement('li');
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'picker-item';
    btn.setAttribute('data-kind', 'dir');
    btn.title = path;

    var label = document.createElement('span');
    label.className = 'picker-name';
    label.textContent = name;
    btn.appendChild(label);

    btn.addEventListener('click', function () { loadDir(path); });
    li.appendChild(btn);
    return li;
  }

  $('#picker-close').addEventListener('click', closePicker);
  $('#picker').addEventListener('click', function (ev) {
    if (ev.target === $('#picker')) closePicker();
  });
  $('#picker-up').addEventListener('click', function () {
    if (picker.parent) loadDir(picker.parent);
  });
  $('#picker-refresh').addEventListener('click', function () {
    loadDir(picker.path);
  });
  $('#picker-use').addEventListener('click', function () {
    if (!picker.path) return;
    dirInput.value = picker.path;
    cfgDirty = true;
    setContentStatus('目录已填入，直接点「开始推送」即可');
    closePicker();
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !$('#picker').hidden) {
      ev.preventDefault();
      closePicker();
    }
  });

  $('#btn-browse').addEventListener('click', openPicker);

  /* ---------------- 设备 ---------------- */

  function fillSerialOptions(keep) {
    if (cfgDirty) return;
    var sel = $('#cfg-serial');
    var value = (keep === undefined) ? sel.value : keep;
    sel.textContent = '';

    var auto = document.createElement('option');
    auto.value = '';
    auto.textContent = '自动（唯一在线设备）';
    sel.appendChild(auto);

    var seen = false;
    devices.forEach(function (d) {
      var opt = document.createElement('option');
      opt.value = d.serial;
      opt.textContent = d.serial + (d.model ? ' · ' + d.model : '') +
        (d.state === 'device' ? '' : '（' + (DEVICE_STATE[d.state] || d.state) + '）');
      sel.appendChild(opt);
      if (d.serial === value) seen = true;
    });

    if (value && !seen) {
      var saved = document.createElement('option');
      saved.value = value;
      saved.textContent = value + '（当前已保存，未连接）';
      sel.appendChild(saved);
    }
    sel.value = value;
  }

  function refreshDevices() {
    return client.devices().then(function (data) {
      devices = data.devices || [];
      fillSerialOptions();
      updateTargetLabel();
    }).catch(function () { /* 后端没起来时静默，连接状态会提示 */ });
  }

  $('#btn-refresh').addEventListener('click', function () {
    cfgDirty = false;
    refreshDevices();
  });

  /* ---------------- 配置 ---------------- */

  var form = $('#config-form');

  function collectConfig() {
    return {
      adbPath: $('#cfg-adb').value.trim(),
      serial: $('#cfg-serial').value,
      pushDir: dirInput.value.trim(),
      remount: $('#cfg-remount').checked
    };
  }

  function applyConfig(cfg, force) {
    if (!cfg) return;
    if (cfgDirty && !force) return;
    $('#cfg-adb').value = cfg.adbPath || '';
    dirInput.value = cfg.pushDir || '';
    $('#cfg-remount').checked = !!cfg.remount;
    fillSerialOptions(cfg.serial || '');
    cfgDirty = false;
  }

  function markDirty() { cfgDirty = true; }
  form.addEventListener('input', markDirty);
  form.addEventListener('change', markDirty);
  dirInput.addEventListener('input', markDirty);

  // 表单里只有一个文本框，回车会触发隐式提交；这里接住，别让页面重载
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (validateDir()) saveConfig();
  });

  // 常用目录只是快捷填入，仍然要保存才生效
  var chips = document.querySelectorAll('.push-chips button');
  for (var i = 0; i < chips.length; i++) {
    chips[i].addEventListener('click', function () {
      dirInput.value = this.getAttribute('data-dir') || '';
      dirInput.removeAttribute('data-tone');
      cfgDirty = true;
      dirInput.focus();
    });
  }

  var cfgStatusTimer = null;

  function setCfgStatus(text, isError) {
    var el = $('#cfg-status');
    if (!el) return;
    el.removeAttribute('data-tone');
    if (isError) el.setAttribute('data-tone', 'error');
    el.textContent = text || '';
    if (cfgStatusTimer) clearTimeout(cfgStatusTimer);
    if (text) {
      cfgStatusTimer = setTimeout(function () { el.textContent = ''; }, 5000);
    }
  }

  function saveConfig() {
    return client.saveConfig(collectConfig()).then(function (data) {
      if (data && data.ok) {
        applyConfig(data.config, true);
        setCfgStatus(data.saved ? '已保存' : '已生效，但写入配置文件失败', !data.saved);
        return true;
      }
      setCfgStatus((data && (data.error || data.message)) || '保存失败', true);
      return false;
    }).catch(function () {
      setCfgStatus('请求失败，后端还在吗？', true);
      return false;
    });
  }

  // 在这两个输入框里回车 = 保存，不要把页面刷新掉
  $('#cfg-adb').addEventListener('keydown', submitOnEnter);
  dirInput.addEventListener('keydown', submitOnEnter);

  function submitOnEnter(ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    if (validateDir()) saveConfig();
  }

  dirInput.addEventListener('blur', function () {
    var value = dirInput.value.trim();
    if (value && value.charAt(0) !== '/') {
      dirInput.setAttribute('data-tone', 'error');
    } else {
      dirInput.removeAttribute('data-tone');
    }
  });

  function validateDir() {
    var value = dirInput.value.trim();
    if (value && value.charAt(0) !== '/') {
      setContentStatus('推送目录必须是 / 开头的绝对路径', true);
      dirInput.focus();
      return false;
    }
    return true;
  }

  /* ---------------- 执行 ---------------- */

  // 页头只有一个主按钮，推的是当前标签里的东西
  $('#btn-run').addEventListener('click', function () {
    if (uploadBusy) {
      setContentStatus('文件还在上传，等传完再推送', true);
      return;
    }
    if (mode === 'preset') {
      runPreset();
      return;
    }
    if (!files.length) {
      setContentStatus('先添加要推送的文件', true);
      return;
    }
    if (!validateDir()) return;

    var btn = $('#btn-run');
    btn.disabled = true;
    // 先落盘参数，再启动：后端执行时读的就是页面上这一份
    client.saveConfig(collectConfig()).then(function (data) {
      if (data && data.ok === false) {
        setContentStatus(
          (data && (data.error || data.message)) || '保存参数失败', true);
        return null;
      }
      if (data && data.config) applyConfig(data.config, true);
      return client.start({});
    }).then(function (data) {
      if (data && !data.ok) setContentStatus(data.message || '启动失败', true);
    }).catch(function () {
      setContentStatus('请求失败，后端还在吗？', true);
    }).then(function () {
      syncButtons();
    });
  });

  $('#btn-stop').addEventListener('click', function () { client.stop(); });

  $('#btn-clear').addEventListener('click', function () {
    log.clear();
    ADB.postJSON('/api/push_file/logs/clear', {});
  });

  /* ---------------- 启动 ---------------- */

  setMode(mode, true);
  refreshDevices();
  setInterval(function () {
    if (!state || state.status === 'idle') refreshDevices();
  }, 15000);
})();
