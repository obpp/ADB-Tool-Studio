/* 替换镜像 二级页面 */
(function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var DEVICE_STATE = {
    'device': '已连接', 'unauthorized': '未授权', 'offline': '离线',
    'no permissions': '无权限', 'recovery': 'recovery',
    'sideload': 'sideload', 'bootloader': 'fastboot'
  };

  var client = new ADB.ToolClient('replace_image');
  var log = new ADB.LogConsole($('#log'), $('#follow'));

  var state = null;
  var presets = [];
  var devices = [];
  var selectedId = '';       // 执行按钮会跑的预设
  var runningId = '';        // 正在烧写的预设
  var editingId = '';        // 表单正在编辑的预设，空 = 新建
  var paramsDirty = false;   // 参数表单改过，别被 SSE 冲掉
  var formDirty = false;     // 预设表单改过，没保存前不许执行
  var renderKey = '';
  var lastBrowseDir = '';
  var confirming = false;

  client.onState(renderState);
  client.onLog(function (entry) { log.append(entry); });

  client.connect(function (snap) {
    applyParams(snap.config, true);
    log.clear();
    log.appendAll(snap.history);
  });

  /* ---------------- 状态渲染 ---------------- */

  function renderState(s) {
    state = s;
    var stats = s.stats || {};
    var extra = s.extra || {};

    presets = extra.presets || [];
    selectedId = extra.selected || '';
    runningId = extra.runningId || '';
    devices = extra.devices || [];
    lastBrowseDir = extra.lastBrowseDir || '';

    applyParams(s.config, false);

    var override = null;
    if (s.status === 'idle' && stats.lastExitCode) {
      override = { tone: 'error', icon: '⚠', text: '上次烧写失败' };
    }
    ADB.paintBadge($('#badge'), s.status, override);

    var current = findPreset(selectedId);
    setText('#meta-preset', current ? current.name : '—');
    setText('#meta-image', current ? baseName(current.imagePath) : '—');
    $('#meta-image').title = current ? current.imagePath : '';
    setText('#meta-target', current ? current.target : '—');
    $('#kpi-runs').textContent = ADB.num(stats.runs);
    $('#kpi-failures').textContent = ADB.num(stats.failures);
    $('#meta-exit').textContent = (stats.lastExitCode === null ||
      stats.lastExitCode === undefined) ? '—' : stats.lastExitCode;
    $('#meta-elapsed').textContent = stats.lastDuration
      ? stats.lastDuration.toFixed(1) + ' 秒' : '—';

    updateDeviceLabel();

    // 第一次打开页面（或别的窗口换了选择）时，把选中的预设铺到表单里。
    // 表单有未保存的修改就别动它。
    if (!formDirty) {
      if (selectedId && editingId !== selectedId) {
        var fill = findPreset(selectedId);
        if (fill) {
          editingId = selectedId;
          fillPresetForm(fill);
          refreshImageInfo(fill.imagePath);
        }
      } else if (!selectedId && editingId && !findPreset(editingId)) {
        // 正在编辑的预设已经不在了
        editingId = '';
        fillPresetForm(blankPreset());
        setImageInfo('');
      }
    }

    renderPresets();
    syncButtons();

    // 已经在烧写了，确认条没有意义
    if (s.status !== 'idle' && confirming) hideConfirm();
  }

  function syncButtons() {
    var running = !!state && state.status !== 'idle';
    $('#btn-run').disabled = running || !selectedId;
    $('#btn-run').title = selectedId ? '' : '先在左边选一个预设';
    $('#btn-stop').disabled = !running;
  }

  function updateDeviceLabel() {
    var el = $('#meta-device');
    var cfg = state && state.config;
    if (!cfg) { el.textContent = '—'; return; }
    if (cfg.serial) { el.textContent = cfg.serial; return; }
    var online = devices.filter(function (d) { return d.state === 'device'; });
    if (online.length === 1) el.textContent = online[0].serial + '（自动）';
    else if (online.length === 0) el.textContent = '无在线设备';
    else el.textContent = online.length + ' 台在线，未选择';
  }

  function resolveSerial() {
    var cfg = state && state.config;
    if (!cfg) return '';
    if (cfg.serial) return cfg.serial;
    var online = devices.filter(function (d) { return d.state === 'device'; });
    return online.length === 1 ? online[0].serial : '';
  }

  setInterval(function () {
    var el = $('#meta-uptime');
    if (!state || !state.startedAt || state.status === 'idle') { el.textContent = '—'; return; }
    el.textContent = ADB.duration(Date.now() / 1000 - state.startedAt);
  }, 1000);

  /* ---------------- 预设列表 ---------------- */

  function findPreset(id) {
    for (var i = 0; i < presets.length; i++) {
      if (presets[i].id === id) return presets[i];
    }
    return null;
  }

  function renderPresets() {
    var list = $('#preset-list');
    if (!list) return;

    var key = presets.map(function (p) {
      return [p.id, p.name, p.imagePath, p.target, p.pushDir, p.reboot,
        p.lastRunAt, p.lastExitCode].join('|');
    }).join('\n') + '#' + selectedId + '#' + runningId;
    if (key === renderKey) return;
    renderKey = key;

    list.textContent = '';

    if (!presets.length) {
      var empty = document.createElement('li');
      empty.className = 'dev-empty';
      empty.textContent = '还没有预设，在下面填好内容点「保存预设」';
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
    var bits = [baseName(item.imagePath) || '未选镜像', '→', item.target || '未设目标'];
    if (item.lastRunAt) {
      bits.push('上次 ' + dateText(item.lastRunAt) +
        (item.lastExitCode === null ? ' 异常' : ' 退出码 ' + item.lastExitCode));
    }
    meta.textContent = bits.join(' ');

    li.appendChild(name);
    li.appendChild(meta);

    li.title = item.name + '\n' + item.imagePath + '\n写入 ' + item.target +
      '\n推送目录 ' + item.pushDir + (item.reboot ? '\n完成后重启' : '\n不重启');
    li.addEventListener('click', function () { selectPreset(item.id); });
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
    editingId = id;
    fillPresetForm(preset);
    formDirty = false;
    setStatus('#form-status', '');
    hideConfirm();
    refreshImageInfo(preset.imagePath);

    if (id !== selectedId) {
      selectedId = id;
      client.post('config', { selected: id });
    }
    renderKey = '';
    renderPresets();
    syncButtons();
  }

  /* ---------------- 预设表单 ---------------- */

  function fillPresetForm(preset) {
    $('#preset-name').value = preset.name || '';
    $('#preset-image').value = preset.imagePath || '';
    $('#preset-pushdir').value = preset.pushDir || '/mnt';
    $('#preset-target').value = preset.target || '';
    $('#preset-reboot').checked = preset.reboot !== false;
    $('#btn-delete').disabled = !editingId;
  }

  function blankPreset() {
    return {
      name: '', imagePath: '', pushDir: '/mnt',
      target: '/dev/block/by-name/boot', reboot: true
    };
  }

  function collectPreset() {
    return {
      id: editingId,
      name: $('#preset-name').value.trim(),
      imagePath: $('#preset-image').value.trim(),
      pushDir: $('#preset-pushdir').value.trim(),
      target: $('#preset-target').value.trim(),
      reboot: $('#preset-reboot').checked
    };
  }

  var presetForm = $('#preset-form');

  presetForm.addEventListener('input', function () {
    formDirty = true;
    setStatus('#form-status', '有未保存的修改');
    hideConfirm();
  });
  presetForm.addEventListener('change', function () {
    formDirty = true;
    setStatus('#form-status', '有未保存的修改');
    hideConfirm();
  });

  presetForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    savePreset();
  });

  function savePreset() {
    var payload = collectPreset();
    if (!payload.imagePath) {
      setStatus('#form-status', '先选一个镜像文件', true);
      return;
    }
    if (!payload.target) {
      setStatus('#form-status', '填一个写入目标', true);
      $('#preset-target').focus();
      return;
    }

    var btn = $('#btn-save');
    btn.disabled = true;
    setStatus('#form-status', '保存中…');

    client.post('presets/save', payload).then(function (data) {
      btn.disabled = false;
      if (!data || !data.ok) {
        setStatus('#form-status', (data && data.error) || '保存失败', true);
        return;
      }
      editingId = data.preset.id;
      selectedId = editingId;
      presets = data.presets || presets;
      formDirty = false;
      fillPresetForm(data.preset);
      renderKey = '';
      renderPresets();
      syncButtons();
      refreshImageInfo(data.preset.imagePath);
      setStatus('#form-status',
        data.saved ? '已保存' : '已生效，但写入配置文件失败', !data.saved);
    }).catch(function () {
      btn.disabled = false;
      setStatus('#form-status', '请求失败，后端还在吗？', true);
    });
  }

  $('#btn-new').addEventListener('click', function () {
    editingId = '';
    selectedId = '';
    fillPresetForm(blankPreset());
    formDirty = false;
    renderKey = '';
    renderPresets();
    syncButtons();
    hideConfirm();
    setStatus('#form-status', '');
    setImageInfo('');
    client.post('config', { selected: '' });
    $('#preset-name').focus();
  });

  $('#btn-delete').addEventListener('click', function () {
    if (!editingId) return;
    var btn = $('#btn-delete');

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
        setStatus('#form-status', (data && data.error) || '删除失败', true);
        return;
      }
      var rest = presets.filter(function (p) { return p.id !== deletedId; });
      editingId = rest.length ? rest[0].id : '';
      selectedId = editingId;
      presets = rest;
      fillPresetForm(rest.length ? rest[0] : blankPreset());
      formDirty = false;
      renderKey = '';
      renderPresets();
      syncButtons();
      refreshImageInfo(rest.length ? rest[0].imagePath : '');
      setStatus('#form-status', '已删除');
    }).catch(function () {
      btn.disabled = false;
      setStatus('#form-status', '请求失败，后端还在吗？', true);
    });
  });

  /* ---------------- 镜像信息 ---------------- */

  function refreshImageInfo(path) {
    path = (path || '').trim();
    if (!path) { setImageInfo(''); return; }
    ADB.postJSON('/api/replace_image/fs/stat', { path: path }).then(function (data) {
      if (!data || !data.ok) { setImageInfo(''); return; }
      if (!data.exists) setImageInfo('文件不存在，执行前请确认路径', true);
      else if (!data.isFile) setImageInfo('这是个目录，不是镜像文件', true);
      else setImageInfo(sizeText(data.size));
    }).catch(function () { setImageInfo(''); });
  }

  function setImageInfo(text, isError) {
    var el = $('#image-info');
    if (!el) return;
    if (isError) el.setAttribute('data-tone', 'error');
    else el.removeAttribute('data-tone');
    el.textContent = text || '';
  }

  $('#preset-image').addEventListener('change', function () {
    refreshImageInfo(this.value);
  });

  /* ---------------- 镜像浏览器 ---------------- */

  var picker = { path: '', parent: '', showAll: false };

  function openPicker() {
    var start = $('#preset-image').value.trim();
    if (start) start = dirName(start);
    if (!start) start = lastBrowseDir;
    $('#picker').hidden = false;
    $('#picker-all').checked = picker.showAll;
    loadDir(start);
    $('#picker-close').focus();
  }

  function closePicker() {
    $('#picker').hidden = true;
    $('#btn-browse').focus();
  }

  function loadDir(path) {
    $('#picker-error').textContent = '正在读取…';
    $('#picker-list').textContent = '';

    ADB.postJSON('/api/replace_image/fs/list', {
      path: path || '',
      all: picker.showAll
    }).then(function (data) {
      if (!data || !data.ok) {
        $('#picker-error').textContent = (data && data.error) || '读不了这个目录';
        if (data && data.path) {
          picker.path = data.path;
          $('#picker-path').value = data.path;
        }
        return;
      }
      picker.path = data.path;
      picker.parent = data.parent || '';
      $('#picker-path').value = data.path;
      $('#picker-error').textContent = data.truncated
        ? '条目太多，只显示了前 2000 项' : '';
      renderPicker(data);
    }).catch(function () {
      $('#picker-error').textContent = '请求失败，后端还在吗？';
    });
  }

  function renderPicker(data) {
    var list = $('#picker-list');
    list.textContent = '';

    if (data.parent) list.appendChild(pickerRow('dir', '..', data.parent, 0));
    (data.dirs || []).forEach(function (d) {
      list.appendChild(pickerRow('dir', d.name, d.path, 0));
    });
    (data.files || []).forEach(function (f) {
      list.appendChild(pickerRow('file', f.name, f.path, f.size));
    });

    if (!(data.dirs || []).length && !(data.files || []).length) {
      var empty = document.createElement('li');
      empty.className = 'picker-empty';
      empty.textContent = picker.showAll
        ? '这个目录是空的'
        : '这里没有镜像文件（.img / .bin / .raw / .iso），可以勾「显示全部文件」';
      list.appendChild(empty);
    }
  }

  function pickerRow(kind, name, path, size) {
    var li = document.createElement('li');
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'picker-item';
    btn.setAttribute('data-kind', kind);
    btn.title = path;

    var label = document.createElement('span');
    label.className = 'picker-name';
    label.textContent = name;
    btn.appendChild(label);

    if (kind === 'file') {
      var sz = document.createElement('span');
      sz.className = 'picker-size';
      sz.textContent = sizeText(size);
      btn.appendChild(sz);
    }

    btn.addEventListener('click', function () {
      if (kind === 'dir') loadDir(path);
      else chooseImage(path);
    });

    li.appendChild(btn);
    return li;
  }

  function chooseImage(path) {
    $('#preset-image').value = path;
    autoName(path);
    formDirty = true;
    setStatus('#form-status', '有未保存的修改');
    hideConfirm();
    refreshImageInfo(path);
    lastBrowseDir = dirName(path);
    closePicker();
    client.post('config', { lastBrowseDir: lastBrowseDir });
  }

  /* 名称只是列表里的标签，留空没意义：从文件名带一个出来，用户还能改 */
  function autoName(path) {
    var input = $('#preset-name');
    if (!input || input.value.trim()) return;
    var base = String(path).split(/[\\/]/).pop() || '';
    var stem = base.replace(/\.[^.]+$/, '');
    if (stem) input.value = stem.slice(0, 60);
  }

  /* 桌面版（pywebview）里有原生桥，能直接弹系统的文件对话框并拿到完整路径。
     网页版没有这座桥 —— 浏览器不会把完整路径告诉页面，只能退回目录浏览器。 */
  function nativePicker() {
    var api = window.pywebview && window.pywebview.api;
    return api && typeof api.pick_image === 'function' ? api : null;
  }

  $('#btn-browse').addEventListener('click', function () {
    var api = nativePicker();
    if (!api) { openPicker(); return; }
    var current = $('#preset-image').value.trim();
    var startDir = current ? dirName(current) : (lastBrowseDir || '');
    api.pick_image(startDir).then(function (path) {
      if (path) chooseImage(path);
    }).catch(function () { openPicker(); });
  });
  $('#picker-close').addEventListener('click', closePicker);
  $('#picker').addEventListener('click', function (ev) {
    if (ev.target === $('#picker')) closePicker();
  });
  $('#picker-up').addEventListener('click', function () {
    if (picker.parent) loadDir(picker.parent);
  });
  $('#picker-home').addEventListener('click', function () {
    // 空路径 = 服务端从用户主目录开始
    loadDir('');
  });
  $('#picker-all').addEventListener('change', function () {
    picker.showAll = this.checked;
    loadDir(picker.path);
  });
  $('#picker-path').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      loadDir(this.value.trim());
    }
    if (ev.key === 'Escape') {
      ev.preventDefault();
      closePicker();
    }
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !$('#picker').hidden) {
      ev.preventDefault();
      closePicker();
    }
  });

  /* ---------------- 设备 ---------------- */

  function fillSerialOptions(keep) {
    if (paramsDirty) return;
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
      devices = (data && data.devices) || [];
      fillSerialOptions();
      updateDeviceLabel();
    }).catch(function () { /* 后端没起来时静默，连接状态会提示 */ });
  }

  $('#btn-refresh').addEventListener('click', function () {
    paramsDirty = false;
    refreshDevices();
  });

  /* ---------------- 参数 ---------------- */

  var configForm = $('#config-form');
  var cfgStatus = $('#cfg-status');

  function collectParams() {
    return {
      adbPath: $('#cfg-adb').value.trim(),
      serial: $('#cfg-serial').value,
      timeout: ADB.numberValue('#cfg-timeout', 600)
    };
  }

  function applyParams(cfg, force) {
    if (!cfg) return;
    if (paramsDirty && !force) return;
    $('#cfg-adb').value = cfg.adbPath || '';
    $('#cfg-timeout').value = cfg.timeout;
    fillSerialOptions(cfg.serial || '');
    paramsDirty = false;
  }

  configForm.addEventListener('input', function () { paramsDirty = true; });
  configForm.addEventListener('change', function () { paramsDirty = true; });

  configForm.addEventListener('submit', function (ev) {
    ev.preventDefault();
    cfgStatus.removeAttribute('data-tone');
    cfgStatus.textContent = '保存中…';
    client.saveConfig(collectParams()).then(function (data) {
      if (data.ok) {
        applyParams(data.config, true);
        cfgStatus.textContent = data.saved ? '已保存' : '已生效，但写入配置文件失败';
        if (!data.saved) cfgStatus.setAttribute('data-tone', 'error');
        setTimeout(function () { cfgStatus.textContent = ''; }, 3000);
      } else {
        cfgStatus.setAttribute('data-tone', 'error');
        cfgStatus.textContent = '保存失败';
      }
    }).catch(function () {
      cfgStatus.setAttribute('data-tone', 'error');
      cfgStatus.textContent = '请求失败，后端还在吗？';
    });
  });

  /* ---------------- 确认条 & 执行 ---------------- */

  function showConfirm(preset, serial) {
    var text = $('#confirm-text');
    text.textContent = '';

    text.appendChild(document.createTextNode('即将把 '));
    text.appendChild(code(baseName(preset.imagePath)));
    text.appendChild(document.createTextNode(' 推送到 '));
    text.appendChild(code(serial + ':' + preset.pushDir));
    text.appendChild(document.createTextNode('，然后写入 '));
    text.appendChild(code(preset.target));
    text.appendChild(document.createTextNode(
      preset.reboot ? '，写入成功后重启设备。' : '，写完不重启。'));

    confirming = true;
    $('#confirm').hidden = false;
    $('#btn-confirm-run').focus();
  }

  function hideConfirm() {
    confirming = false;
    $('#confirm').hidden = true;
  }

  function code(text) {
    var el = document.createElement('code');
    el.textContent = text;
    return el;
  }

  $('#btn-run').addEventListener('click', function () {
    if (!selectedId) {
      setStatus('#preset-status', '先在左边选一个预设', true);
      return;
    }
    if (formDirty) {
      setStatus('#preset-status', '预设有未保存的修改，先保存再执行', true);
      return;
    }
    var preset = findPreset(selectedId);
    if (!preset) {
      setStatus('#preset-status', '这个预设已经不在了，刷新一下', true);
      return;
    }
    var serial = resolveSerial();
    if (!serial) {
      setStatus('#preset-status', '没有可用的在线设备，检查数据线和 USB 调试授权', true);
      return;
    }
    showConfirm(preset, serial);
  });

  $('#btn-confirm-cancel').addEventListener('click', function () {
    hideConfirm();
    $('#btn-run').focus();
  });

  $('#btn-confirm-run').addEventListener('click', function () {
    var presetId = selectedId;
    hideConfirm();
    // 先落盘参数，免得烧写用的还是上一次的 adb 路径或设备
    client.saveConfig(collectParams()).then(function () {
      return client.start({ presetId: presetId });
    }).then(function (data) {
      if (data && !data.ok) {
        setStatus('#preset-status', data.message || '启动失败', true);
      }
    }).catch(function () {
      setStatus('#preset-status', '请求失败，后端还在吗？', true);
    });
  });

  $('#btn-stop').addEventListener('click', function () { client.stop(); });

  $('#btn-clear').addEventListener('click', function () {
    log.clear();
    ADB.postJSON('/api/replace_image/logs/clear', {});
  });

  /* ---------------- 小工具 ---------------- */

  function setText(sel, text) { $(sel).textContent = text; }

  function baseName(path) {
    if (!path) return '';
    var parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || path;
  }

  function dirName(path) {
    var i = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
    if (i < 0) return '';
    return path.slice(0, i) || '/';
  }

  function sizeText(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
    return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
  }

  function dateText(epochSeconds) {
    if (!epochSeconds) return '';
    var d = new Date(epochSeconds * 1000);
    return (d.getMonth() + 1) + '-' + d.getDate() + ' ' +
      ADB.pad(d.getHours()) + ':' + ADB.pad(d.getMinutes());
  }

  var statusTimers = {};

  function setStatus(sel, text, isError) {
    var el = $(sel);
    if (!el) return;
    if (isError) el.setAttribute('data-tone', 'error');
    else el.removeAttribute('data-tone');
    el.textContent = text || '';
    if (statusTimers[sel]) clearTimeout(statusTimers[sel]);
    if (text) {
      statusTimers[sel] = setTimeout(function () {
        el.textContent = '';
        el.removeAttribute('data-tone');
      }, 4000);
    }
  }

  /* ---------------- 启动 ---------------- */

  refreshDevices();
  setInterval(function () {
    if (!state || state.status === 'idle') refreshDevices();
  }, 15000);
})();
