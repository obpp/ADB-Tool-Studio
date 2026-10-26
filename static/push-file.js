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
  var renderKey = '';        // 清单没变就不重画

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

    var override = null;
    if (s.status === 'idle' && stats.lastExitCode) {
      override = { tone: 'error', icon: '⚠', text: '上次推送失败' };
    }
    ADB.paintBadge($('#badge'), s.status, override);

    $('#meta-count').textContent = files.length;
    $('#kpi-pushed').textContent = ADB.num(stats.filesPushed);
    $('#kpi-failed').textContent = ADB.num(stats.filesFailed);
    $('#meta-exit').textContent = (stats.lastExitCode === null ||
      stats.lastExitCode === undefined) ? '—' : stats.lastExitCode;
    $('#meta-elapsed').textContent = stats.lastDuration
      ? stats.lastDuration.toFixed(1) + ' 秒' : '—';
    $('#meta-dir').textContent = (s.config && s.config.pushDir) || '—';

    updateTargetLabel();
    fillSerialOptions();
    renderFiles();
    syncButtons();
  }

  function syncButtons() {
    var running = !!state && state.status !== 'idle';
    $('#btn-run').disabled = running || uploadBusy || !files.length;
    $('#btn-stop').disabled = !running;
    $('#btn-add').disabled = running || uploadBusy;
    $('#btn-clear-files').disabled = running || uploadBusy || !files.length;

    var buttons = $('#file-list').querySelectorAll('button');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = running || uploadBusy;
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
          setCfgStatus(data.error || '移除失败', true);
          del.disabled = false;
          return;
        }
        files = data.files || [];
        renderKey = '';
        renderFiles();
        syncButtons();
      }).catch(function () {
        setCfgStatus('请求失败，后端还在吗？', true);
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

  var fileStatusTimer = null;

  function setFileStatus(text, isError) {
    var el = $('#file-status');
    if (!el) return;
    el.removeAttribute('data-tone');
    if (isError) el.setAttribute('data-tone', 'error');
    el.textContent = text || '';
    if (fileStatusTimer) clearTimeout(fileStatusTimer);
    if (text) {
      fileStatusTimer = setTimeout(function () { el.textContent = ''; }, 5000);
    }
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
      setFileStatus('正在推送，等这次结束再加文件', true);
      return;
    }

    uploadBusy = true;
    syncButtons();
    setFileStatus('正在上传 0/' + pending.length + '…');

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
          setFileStatus('正在上传 ' + done + '/' + pending.length + '…');
        });
      });
    });

    chain.then(function () {
      if (errors.length) {
        setFileStatus('有 ' + errors.length + ' 个文件没传上去：' +
          errors[0] + (errors.length > 1 ? ' 等' : ''), true);
        log.append({ time: clock(), level: 'err',
          msg: '上传失败：' + errors.join('；') });
      } else {
        setFileStatus('已加入 ' + done + ' 个文件');
      }
    }).catch(function () {
      setFileStatus('上传中断', true);
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
        setCfgStatus(data.error || '清空失败', true);
        return;
      }
      files = [];
      renderKey = '';
      renderFiles();
    }).catch(function () {
      setCfgStatus('请求失败，后端还在吗？', true);
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
    if (uploadBusy) { setFileStatus('还在上传上一批文件，稍等', true); return; }
    addFiles(ev.dataTransfer && ev.dataTransfer.files);
  });

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
    setCfgStatus('目录已填入，点「保存参数」或直接开始推送', false);
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
      pushDir: dirInput.value.trim()
    };
  }

  function applyConfig(cfg, force) {
    if (!cfg) return;
    if (cfgDirty && !force) return;
    $('#cfg-adb').value = cfg.adbPath || '';
    dirInput.value = cfg.pushDir || '';
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

  $('#btn-save').addEventListener('click', function () {
    if (!validateDir()) return;
    saveConfig();
  });

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
      setCfgStatus('推送目录必须是 / 开头的绝对路径', true);
      dirInput.focus();
      return false;
    }
    return true;
  }

  /* ---------------- 执行 ---------------- */

  $('#btn-run').addEventListener('click', function () {
    if (uploadBusy) {
      setCfgStatus('文件还在上传，等传完再推送', true);
      return;
    }
    if (!files.length) {
      setCfgStatus('先添加要推送的文件', true);
      return;
    }
    if (!validateDir()) return;

    var btn = $('#btn-run');
    btn.disabled = true;
    // 先落盘参数，再启动：后端执行时读的就是页面上这一份
    client.saveConfig(collectConfig()).then(function (data) {
      if (data && data.ok === false) {
        setCfgStatus((data && (data.error || data.message)) || '保存参数失败', true);
        return null;
      }
      if (data && data.config) applyConfig(data.config, true);
      return client.start({});
    }).then(function (data) {
      if (data && !data.ok) setCfgStatus(data.message || '启动失败', true);
    }).catch(function () {
      setCfgStatus('请求失败，后端还在吗？', true);
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

  refreshDevices();
  setInterval(function () {
    if (!state || state.status === 'idle') refreshDevices();
  }, 15000);
})();
