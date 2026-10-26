/* always-adb 二级页面 */
(function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var DEVICE_STATE = {
    'device': '已连接', 'unauthorized': '未授权', 'offline': '离线',
    'no permissions': '无权限', 'recovery': 'recovery',
    'sideload': 'sideload', 'bootloader': 'fastboot'
  };

  var client = new ADB.ToolClient('always_adb');
  var log = new ADB.LogConsole($('#log'), $('#follow'));

  var devices = [];
  var cfgDirty = false;
  var state = null;

  client.onState(renderState);
  client.onLog(function (entry) { log.append(entry); });

  client.connect(function (snap) {
    devices = (snap.extra && snap.extra.devices) || [];
    renderDevices();
    applyConfig(snap.config, true);
    log.clear();
    log.appendAll(snap.history);
  });

  /* ---------------- 状态渲染 ---------------- */

  function renderState(s) {
    state = s;
    var stats = s.stats || {};
    var extra = s.extra || {};

    var override = null;
    if (s.status === 'idle' && stats.consecutiveFailures > 0) {
      override = { tone: 'error', icon: '⚠', text: '已停止（上次失败）' };
    }
    ADB.paintBadge($('#badge'), s.status, override);

    $('#meta-mode').textContent = extra.mode === 'once' ? '单次执行'
      : (extra.mode === 'loop' ? '持续循环' : '—');
    $('#meta-last').textContent = ADB.clockTime(stats.lastSuccessAt);

    $('#kpi-rounds').textContent = ADB.num(stats.rounds);
    $('#kpi-ok').textContent = ADB.num(Math.max(0, (stats.attempts || 0) - (stats.retries || 0)));
    $('#kpi-retries').textContent = ADB.num(stats.retries);

    var fails = stats.consecutiveFailures || 0;
    $('#kpi-fails').textContent = ADB.num(fails);
    var tile = $('#kpi-fail-tile');
    if (fails === 0) {
      tile.setAttribute('data-tone', 'ok');
      $('#kpi-fails-hint').textContent = '';
    } else if (fails < 3) {
      tile.setAttribute('data-tone', 'warning');
      $('#kpi-fails-hint').textContent = '重试中';
    } else {
      tile.setAttribute('data-tone', 'critical');
      $('#kpi-fails-hint').textContent = '请检查连接与调试授权';
    }

    // 长驻监听会在设备上线/掉线时推 state，列表和状态栏都跟着立刻更新
    devices = extra.devices || devices;
    renderDevices();
    renderTarget(s.config, devices);
    fillSerialOptions(s.config ? s.config.serial : '');

    var running = s.status !== 'idle';
    $('#btn-start').disabled = running;
    $('#btn-once').disabled = running;
    $('#btn-stop').disabled = !running;
  }

  function renderTarget(config, list) {
    var el = $('#meta-target');
    if (!config) { el.textContent = '—'; return; }
    if (config.serial) { el.textContent = config.serial; return; }
    var online = (list || []).filter(function (d) { return d.state === 'device'; });
    if (online.length === 1) el.textContent = online[0].serial + '（自动识别）';
    else if (online.length === 0) el.textContent = '无在线设备';
    else el.textContent = online.length + ' 台在线，未指定序列号';
  }

  // “已运行”每秒本地走字，不靠服务端推送
  setInterval(function () {
    var el = $('#meta-uptime');
    if (!state || !state.startedAt || state.status === 'idle') { el.textContent = '—'; return; }
    el.textContent = ADB.duration(Date.now() / 1000 - state.startedAt);
  }, 1000);

  /* ---------------- 设备列表 ---------------- */

  // 只有 state=device 才算真的能用；其余状态标成警告色，眼睛一扫就能看出哪台有问题
  function deviceTone(st) {
    if (st === 'device') return 'ok';
    if (st === 'unauthorized' || st === 'offline') return 'warn';
    return 'error';
  }

  function renderDevices() {
    var list = $('#device-list');
    if (!list) return;
    list.textContent = '';

    if (!devices.length) {
      var empty = document.createElement('li');
      empty.className = 'dev-empty';
      empty.textContent = '没有检测到设备。检查数据线和 USB 调试授权。';
      list.appendChild(empty);
      return;
    }

    devices.forEach(function (d) {
      var li = document.createElement('li');
      li.className = 'dev';

      var lamp = document.createElement('span');
      lamp.className = 'lamp';
      lamp.setAttribute('data-tone', deviceTone(d.state));

      var serial = document.createElement('span');
      serial.className = 'dev-serial';
      serial.textContent = d.serial;

      var st = document.createElement('span');
      st.className = 'dev-state';
      st.textContent = DEVICE_STATE[d.state] || d.state;

      var model = document.createElement('span');
      model.className = 'dev-model';
      model.textContent = d.model || d.product || '—';

      li.title = d.serial + '\n' + st.textContent + '\n型号：' + (d.model || '—') +
        '\n产品：' + (d.product || '—');

      li.appendChild(lamp);
      li.appendChild(serial);
      li.appendChild(st);
      li.appendChild(model);
      list.appendChild(li);
    });
  }

  function refreshDevices() {
    return client.devices().then(function (data) {
      devices = data.devices || [];
      renderDevices();
      if (state) renderTarget(state.config, devices);
      fillSerialOptions(state && state.config ? state.config.serial : '');
    }).catch(function () { /* 后端没起来时静默，连接状态会提示 */ });
  }

  function fillSerialOptions(selected) {
    if (cfgDirty) return;
    var sel = $('#cfg-serial');
    var keep = selected || sel.value || '';
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
      if (d.serial === keep) seen = true;
    });

    if (keep && !seen) {
      var extra = document.createElement('option');
      extra.value = keep;
      extra.textContent = keep + '（当前已保存，未连接）';
      sel.appendChild(extra);
    }
    sel.value = keep;
  }

  $('#btn-refresh').addEventListener('click', refreshDevices);

  /* ---------------- 配置 ---------------- */

  var form = $('#config-form');
  var cfgStatus = $('#cfg-status');

  function collectConfig() {
    return {
      adbPath: $('#cfg-adb').value.trim(),
      serial: $('#cfg-serial').value,
      forceAdbValue: $('#cfg-value').value.trim() || '1',
      retryInterval: ADB.numberValue('#cfg-retry', 1),
      waitSeconds: ADB.numberValue('#cfg-wait', 10),
      commandTimeout: ADB.numberValue('#cfg-timeout', 30),
      doRoot: $('#cfg-doroot').checked,
      doSync: $('#cfg-dosync').checked,
      waitAfterRoot: $('#cfg-waitroot').checked
    };
  }

  function applyConfig(cfg, force) {
    if (!cfg) return;
    if (cfgDirty && !force) return;
    $('#cfg-adb').value = cfg.adbPath || '';
    $('#cfg-value').value = cfg.forceAdbValue === undefined ? '1' : cfg.forceAdbValue;
    $('#cfg-retry').value = cfg.retryInterval;
    $('#cfg-wait').value = cfg.waitSeconds;
    $('#cfg-timeout').value = cfg.commandTimeout;
    $('#cfg-doroot').checked = !!cfg.doRoot;
    $('#cfg-dosync').checked = !!cfg.doSync;
    $('#cfg-waitroot').checked = !!cfg.waitAfterRoot;
    fillSerialOptions(cfg.serial || '');
    cfgDirty = false;
  }

  form.addEventListener('input', function () { cfgDirty = true; });
  form.addEventListener('change', function () { cfgDirty = true; });

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

  /* ---------------- 控制按钮 ---------------- */

  // 先落盘参数，避免“改了没保存就点开始”
  function startWith(options) {
    return client.saveConfig(collectConfig())
      .then(refreshDevices)
      .then(function () { return client.start(options); });
  }

  $('#btn-start').addEventListener('click', function () { startWith({ once: false }); });
  $('#btn-once').addEventListener('click', function () { startWith({ once: true }); });
  $('#btn-stop').addEventListener('click', function () { client.stop(); });

  $('#btn-clear').addEventListener('click', function () {
    log.clear();
    // 服务端的历史一起清掉，否则刷新页面又冒出来
    ADB.postJSON('/api/always_adb/logs/clear', {});
  });

  /* ---------------- 启动 ---------------- */

  refreshDevices();
  setInterval(function () {
    if (!state || state.status === 'idle') refreshDevices();
  }, 10000);
})();
