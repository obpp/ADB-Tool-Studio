/* 脚本执行 二级页面 */
(function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var MAX_BYTES = 2 * 1024 * 1024;

  var DEVICE_STATE = {
    'device': '已连接', 'unauthorized': '未授权', 'offline': '离线',
    'no permissions': '无权限', 'recovery': 'recovery',
    'sideload': 'sideload', 'bootloader': 'fastboot'
  };

  var client = new ADB.ToolClient('script_runner');
  var log = new ADB.LogConsole($('#log'), $('#follow'));
  var argsInput = $('#cfg-args');

  var state = null;
  var scripts = [];
  var devices = [];
  var selectedId = '';
  var runningId = '';
  var cfgDirty = false;
  var renderKey = '';        // 清单没变就不重画，免得把正在改名的输入框冲掉

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

    scripts = extra.scripts || [];
    selectedId = extra.selected || '';
    runningId = extra.runningId || '';
    // 后端的长驻监听会在设备上线时推 state，这里跟着更新，别等 15 秒轮询
    devices = extra.devices || devices;

    var override = null;
    if (s.status === 'idle' && stats.lastExitCode) {
      override = { tone: 'error', icon: '⚠', text: '上次执行失败' };
    }
    ADB.paintBadge($('#badge'), s.status, override);

    var current = findScript(selectedId);
    $('#meta-script').textContent = current ? current.name : '—';
    syncArgsInput(current);
    $('#kpi-runs').textContent = ADB.num(stats.runs);
    $('#kpi-failures').textContent = ADB.num(stats.failures);
    $('#meta-exit').textContent = (stats.lastExitCode === null ||
      stats.lastExitCode === undefined) ? '—' : stats.lastExitCode;
    $('#meta-elapsed').textContent = stats.lastDuration
      ? stats.lastDuration.toFixed(1) + ' 秒' : '—';

    updateTargetLabel();
    fillSerialOptions();
    renderScripts();
    syncButtons();
  }

  function syncButtons() {
    var running = !!state && state.status !== 'idle';
    $('#btn-run').disabled = running || !selectedId;
    $('#btn-stop').disabled = !running;
  }

  function findScript(id) {
    for (var i = 0; i < scripts.length; i++) {
      if (scripts[i].id === id) return scripts[i];
    }
    return null;
  }

  /* 参数是跟着脚本存的：切换脚本时输入框换成这一份，执行时用输入框里的。
     正在输入时不覆盖，否则状态推送会把用户打的字冲掉。 */
  function syncArgsInput(script) {
    if (!argsInput) return;
    argsInput.disabled = !script;
    if (document.activeElement === argsInput) return;
    var value = script ? (script.args || '') : '';
    if (argsInput.value !== value) argsInput.value = value;
  }

  function saveArgs(id) {
    if (!id || !argsInput) return;
    var text = argsInput.value.trim();
    var script = findScript(id);
    if (script && (script.args || '') === text) return;
    client.post('scripts/args', { id: id, args: text }).then(function (data) {
      if (!data.ok) {
        setStatus(data.error || '参数保存失败', true);
        return;
      }
      // 用发请求时的 id —— 这期间可能已经切到别的脚本了
      var saved = findScript(id);
      if (saved) saved.args = (data.script && data.script.args) || '';
    }).catch(function () { /* 后端没响应时连接状态会提示 */ });
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

  /* ---------------- 脚本列表 ---------------- */

  function sizeText(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function dateText(epochSeconds) {
    if (!epochSeconds) return '';
    var d = new Date(epochSeconds * 1000);
    return (d.getMonth() + 1) + '-' + d.getDate() + ' ' +
      ADB.pad(d.getHours()) + ':' + ADB.pad(d.getMinutes());
  }

  function renderScripts() {
    var list = $('#script-list');
    if (!list) return;
    // 正在改名时不要重画，否则输入框会被整条替换掉
    if (list.querySelector('.script-rename')) return;

    var key = scripts.map(function (s) {
      return [s.id, s.name, s.size, s.args || '', s.lastRunAt, s.lastExitCode].join('|');
    }).join('\n') + '#' + selectedId + '#' + runningId;
    if (key === renderKey) return;
    renderKey = key;

    list.textContent = '';

    if (!scripts.length) {
      var empty = document.createElement('li');
      empty.className = 'dev-empty';
      empty.textContent = '还没有脚本，点「导入脚本…」选一个';
      list.appendChild(empty);
      return;
    }

    scripts.forEach(function (item) {
      list.appendChild(buildRow(item));
    });
  }

  function buildRow(item) {
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
    var bits = [sizeText(item.size)];
    if (item.addedAt) bits.push(dateText(item.addedAt) + ' 导入');
    if (item.lastRunAt) {
      bits.push('上次 ' + (item.lastExitCode === null ? '异常' : '退出码 ' + item.lastExitCode));
    }
    meta.textContent = bits.join(' · ');

    var ops = document.createElement('span');
    ops.className = 'script-ops';

    var rename = document.createElement('button');
    rename.type = 'button';
    rename.className = 'btn ghost small';
    rename.textContent = '改名';
    rename.addEventListener('click', function (ev) {
      ev.stopPropagation();
      startRename(li, name, item);
    });

    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn ghost small';
    del.textContent = '删除';
    del.addEventListener('click', function (ev) {
      ev.stopPropagation();
      armDelete(del, item);
    });

    ops.appendChild(rename);
    ops.appendChild(del);

    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(ops);

    li.title = item.name + '\n' + meta.textContent +
      (item.args ? '\n参数：' + item.args : '');
    li.addEventListener('click', function (ev) {
      if (ev.target.closest && ev.target.closest('button')) return;
      selectScript(item.id);
    });
    li.addEventListener('keydown', function (ev) {
      if (ev.target !== li) return;
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        selectScript(item.id);
      }
    });
    return li;
  }

  function selectScript(id) {
    if (!id || id === selectedId) return;
    selectedId = id;
    renderKey = '';
    renderScripts();
    syncArgsInput(findScript(id));
    syncButtons();
    // 只发 selected，其余字段由服务端沿用当前配置
    client.post('config', { selected: id });
  }

  function startRename(li, nameEl, item) {
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'script-rename';
    input.value = item.name;
    input.setAttribute('aria-label', '脚本名');

    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      var value = input.value.trim();
      if (input.parentNode) input.parentNode.replaceChild(nameEl, input);
      renderKey = '';
      if (save && value && value !== item.name) {
        client.post('scripts/rename', { id: item.id, name: value }).then(function (data) {
          if (!data.ok) setStatus(data.error || '改名失败', true);
          renderKey = '';
        });
      }
      setTimeout(renderScripts, 0);
    }

    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); finish(true); }
      else if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
      ev.stopPropagation();
    });
    input.addEventListener('click', function (ev) { ev.stopPropagation(); });
    // 点到别处也算改完 —— 和资源管理器里的改名一致
    input.addEventListener('blur', function () { finish(true); });

    li.replaceChild(input, nameEl);
    input.focus();
    input.select();
  }

  function armDelete(btn, item) {
    if (btn.getAttribute('data-armed') === '1') {
      btn.removeAttribute('data-armed');
      btn.disabled = true;
      client.post('scripts/delete', { id: item.id }).then(function (data) {
        if (!data.ok) {
          setStatus(data.error || '删除失败', true);
          btn.disabled = false;
          btn.textContent = '删除';
          btn.classList.remove('danger');
        }
        renderKey = '';
      });
      return;
    }
    // 第一次点只是「上膛」，3 秒内再点才真的删
    btn.setAttribute('data-armed', '1');
    btn.textContent = '确认删除';
    btn.classList.add('danger');
    setTimeout(function () {
      if (btn.getAttribute('data-armed') !== '1') return;
      btn.removeAttribute('data-armed');
      btn.textContent = '删除';
      btn.classList.remove('danger');
    }, 3000);
  }

  /* ---------------- 导入 ---------------- */

  var importStatusTimer = null;

  function setStatus(text, isError) {
    var el = $('#script-status');
    if (!el) return;
    el.removeAttribute('data-tone');
    if (isError) el.setAttribute('data-tone', 'error');
    el.textContent = text || '';
    if (importStatusTimer) clearTimeout(importStatusTimer);
    if (text) {
      importStatusTimer = setTimeout(function () { el.textContent = ''; }, 4000);
    }
  }

  function importFile(file) {
    if (!file) return;
    if (file.size > MAX_BYTES) {
      setStatus('文件太大（上限 2 MB）', true);
      return;
    }
    var btn = $('#btn-import');
    btn.disabled = true;
    setStatus('正在导入 ' + file.name + ' …');

    fetch('/api/script_runner/scripts/import?name=' + encodeURIComponent(file.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: 'HTTP ' + r.status };
      });
    }).then(function (data) {
      if (!data.ok) {
        setStatus(data.error || '导入失败', true);
      } else {
        selectedId = data.script.id;
        setStatus('已导入 ' + data.script.name);
        renderKey = '';
        renderScripts();
      }
    }).catch(function () {
      setStatus('请求失败，后端还在吗？', true);
    }).then(function () {
      btn.disabled = false;
    });
  }

  $('#btn-import').addEventListener('click', function () { $('#file-input').click(); });

  $('#file-input').addEventListener('change', function (ev) {
    var file = ev.target.files && ev.target.files[0];
    ev.target.value = '';           // 允许重复导入同一个文件
    importFile(file);
  });

  // 拖放：整个脚本库下方都是投放区，不只是列表本身。
  // dragenter / dragleave 会从子元素冒泡上来，用计数避免划过每一行时闪烁。
  var zone = $('#script-drop');
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
    var file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
    importFile(file);
  });

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
  var cfgStatus = $('#cfg-status');

  function collectConfig() {
    return {
      adbPath: $('#cfg-adb').value.trim(),
      serial: $('#cfg-serial').value,
      remoteDir: $('#cfg-remote').value.trim(),
      timeout: ADB.numberValue('#cfg-timeout', 60)
    };
  }

  function applyConfig(cfg, force) {
    if (!cfg) return;
    if (cfgDirty && !force) return;
    $('#cfg-adb').value = cfg.adbPath || '';
    $('#cfg-remote').value = cfg.remoteDir || '';
    $('#cfg-timeout').value = cfg.timeout;
    fillSerialOptions(cfg.serial || '');
    cfgDirty = false;
  }

  // 脚本参数跟着选中的脚本存，不属于这份全局配置：打字时别把它算成配置改动，
  // 否则设备列表刷新会因为 cfgDirty 停下来
  function markDirty(ev) {
    if (ev.target === argsInput) return;
    cfgDirty = true;
  }
  form.addEventListener('input', markDirty);
  form.addEventListener('change', markDirty);

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    cfgStatus.removeAttribute('data-tone');
    cfgStatus.textContent = '保存中…';
    client.saveConfig(collectConfig()).then(function (data) {
      if (data.ok) {
        applyConfig(data.config, true);
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

  /* ---------------- 执行 ---------------- */

  if (argsInput) {
    // change 在输入框失焦时才触发，正好是「这一串输完了」的时刻
    argsInput.addEventListener('change', function () { saveArgs(selectedId); });
    // 它在「保存参数」那个表单里，回车默认会提交整份全局配置；这里改成只存脚本参数
    argsInput.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter') return;
      ev.preventDefault();
      saveArgs(selectedId);
    });
  }

  $('#btn-run').addEventListener('click', function () {
    if (!selectedId) {
      setStatus('先导入并选一个脚本', true);
      return;
    }
    // 先落盘参数，免得执行用的还是上一次的设置；脚本参数跟着 start 一起发，
    // 后端收到就记到这个脚本名下，执行用的就是页面上这一串
    client.saveConfig(collectConfig()).then(function () {
      return client.start({
        scriptId: selectedId,
        args: argsInput ? argsInput.value.trim() : ''
      });
    }).then(function (data) {
      if (data && !data.ok) setStatus(data.message || '启动失败', true);
    }).catch(function () {
      setStatus('请求失败，后端还在吗？', true);
    });
  });

  $('#btn-stop').addEventListener('click', function () { client.stop(); });

  $('#btn-clear').addEventListener('click', function () {
    log.clear();
    ADB.postJSON('/api/script_runner/logs/clear', {});
  });

  /* ---------------- 启动 ---------------- */

  refreshDevices();
  setInterval(function () {
    if (!state || state.status === 'idle') refreshDevices();
  }, 15000);
})();
