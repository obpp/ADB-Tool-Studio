/* adb 反向隧道 二级页面 */
(function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var client = new ADB.ToolClient('ssh_tunnel');
  var log = new ADB.LogConsole($('#log'), $('#follow'));

  var state = null;
  var cfgDirty = false;
  var lastCommand = '';
  var previewTimer = null;

  client.onState(renderState);
  client.onLog(function (entry) { log.append(entry); });

  client.connect(function (snap) {
    applyConfig(snap.config, true);
    log.clear();
    log.appendAll(snap.history);
    schedulePreview();
  });

  /* ---------------- 状态渲染 ---------------- */

  function badgeFor(s) {
    var extra = s.extra || {};
    // 停掉之后端口占用就不该再顶在徽标上 —— 输入框下面的红字还留着，照样看得见
    if (extra.blockedPort && s.status !== 'idle') {
      return { tone: 'error', icon: '⚠', text: '远程端口 ' + extra.blockedPort + ' 被占用' };
    }
    if (s.status === 'reconnecting') {
      return { tone: 'warn', icon: '↻', text: '重连中' };
    }
    if (s.status === 'stopping') {
      return { tone: 'warn', icon: '⏸', text: '断开中' };
    }
    if (s.status === 'running') {
      return extra.connected
        ? { tone: 'running', icon: '▶', text: '隧道已建立' }
        : { tone: 'warn', icon: '…', text: '启动中' };
    }
    // idle
    if (s.stats && s.stats.lastError) {
      return { tone: 'error', icon: '⚠', text: '上次连接失败' };
    }
    return { tone: 'idle', icon: '■', text: '未建立' };
  }

  function renderState(s) {
    state = s;
    var stats = s.stats || {};
    var extra = s.extra || {};

    ADB.paintBadge($('#badge'), s.status, badgeFor(s));

    $('#meta-tunnel').textContent = extra.remoteSpec
      ? extra.remoteSpec + ' → ' + extra.localSpec : '—';
    $('#meta-exit').textContent = (stats.lastExitCode === null ||
      stats.lastExitCode === undefined) ? '—' : stats.lastExitCode;

    $('#kpi-connects').textContent = ADB.num(stats.connects);
    $('#kpi-failures').textContent = ADB.num(stats.failures);
    $('#kpi-last').textContent = ADB.clockTime(stats.lastConnectedAt);

    // 服务端的命令行是权威版本，顺手同步一下预览
    if (extra.command && !cfgDirty) {
      lastCommand = extra.command;
      $('#cmd-preview').textContent = extra.command;
    }

    var running = s.status !== 'idle';
    $('#btn-start').disabled = running;
    $('#btn-stop').disabled = !running;

    updatePortHint(extra);
  }

  /* 远程端口不是 5037 时，服务端 adb 必须设 ANDROID_ADB_SERVER_PORT；
     端口绑定失败时，优先提示换一个端口。 */
  function updatePortHint(extra) {
    var el = $('#port-hint');
    if (!el) return;
    extra = extra || (state && state.extra) || {};
    var port = ADB.numberValue('#cfg-remoteport', 5037);
    var blocked = Number(extra.blockedPort) || 0;

    el.hidden = false;
    if (blocked && blocked === port) {
      var suggest = port < 65535 ? port + 1 : port - 1;
      el.setAttribute('data-tone', 'error');
      el.textContent = '端口 ' + port + ' 绑定失败（多半被占用）。改一个再启动，比如 ' +
        suggest + '；服务端要同步 export ANDROID_ADB_SERVER_PORT=' + suggest;
      return;
    }

    el.removeAttribute('data-tone');
    if (port === 5037) {
      el.hidden = true;
      el.textContent = '';
      return;
    }
    el.textContent = '服务端 adb 要连这个端口：export ANDROID_ADB_SERVER_PORT=' + port;
  }

  setInterval(function () {
    var el = $('#meta-uptime');
    if (!state || !state.startedAt || state.status === 'idle') { el.textContent = '—'; return; }
    el.textContent = ADB.duration(Date.now() / 1000 - state.startedAt);
  }, 1000);

  /* ---------------- 命令行预览 ---------------- */

  function schedulePreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(updatePreview, 250);
  }

  function updatePreview() {
    previewTimer = null;
    client.preview(collectConfig()).then(function (data) {
      if (data.shell === undefined) return;
      lastCommand = data.shell;
      $('#cmd-preview').textContent = data.shell;
      var warn = $('#cmd-warn');
      if (data.warning) {
        warn.textContent = data.warning;
        warn.hidden = false;
      } else {
        warn.hidden = true;
      }
    }).catch(function () { /* 预览失败不打扰用户 */ });
  }

  $('#btn-copy').addEventListener('click', function () {
    if (!lastCommand) return;
    ADB.copyText(lastCommand, $('#copy-status'), '已复制');
  });

  /* ---------------- 配置 ---------------- */

  var form = $('#config-form');
  var cfgStatus = $('#cfg-status');

  function collectConfig() {
    return {
      sshPath: $('#cfg-ssh').value.trim(),
      host: $('#cfg-host').value.trim(),
      user: $('#cfg-user').value.trim(),
      sshPort: ADB.numberValue('#cfg-sshport', 22),
      identityFile: $('#cfg-identity').value.trim(),
      hostKeyPolicy: $('#cfg-hostkey').value,
      keepAlive: $('#cfg-keepalive').checked,
      batchMode: $('#cfg-batchmode').checked,
      remotePort: ADB.numberValue('#cfg-remoteport', 5037),
      localHost: $('#cfg-localhost').value.trim(),
      localPort: ADB.numberValue('#cfg-localport', 5037),
      gatewayPorts: $('#cfg-gateway').checked,
      autoReconnect: $('#cfg-autoreconnect').checked,
      reconnectInterval: ADB.numberValue('#cfg-reconnect', 3)
    };
  }

  function applyConfig(cfg, force) {
    if (!cfg) return;
    if (cfgDirty && !force) return;
    $('#cfg-ssh').value = cfg.sshPath || '';
    $('#cfg-host').value = cfg.host || '';
    $('#cfg-user').value = cfg.user || '';
    $('#cfg-sshport').value = cfg.sshPort;
    $('#cfg-identity').value = cfg.identityFile || '';
    $('#cfg-hostkey').value = cfg.hostKeyPolicy;
    $('#cfg-keepalive').checked = !!cfg.keepAlive;
    $('#cfg-batchmode').checked = !!cfg.batchMode;
    $('#cfg-remoteport').value = cfg.remotePort;
    $('#cfg-localhost').value = cfg.localHost || '';
    $('#cfg-localport').value = cfg.localPort;
    $('#cfg-gateway').checked = !!cfg.gatewayPorts;
    $('#cfg-autoreconnect').checked = !!cfg.autoReconnect;
    $('#cfg-reconnect').value = cfg.reconnectInterval;
    cfgDirty = false;
  }

  form.addEventListener('input', function () {
    cfgDirty = true;
    schedulePreview();
    updatePortHint();
  });
  form.addEventListener('change', function () {
    cfgDirty = true;
    schedulePreview();
    updatePortHint();
  });

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

  $('#btn-start').addEventListener('click', function () {
    // 先落盘再连，免得连的还是旧参数
    client.saveConfig(collectConfig()).then(function () {
      return client.start();
    });
  });

  $('#btn-stop').addEventListener('click', function () { client.stop(); });

  $('#btn-clear').addEventListener('click', function () {
    log.clear();
    ADB.postJSON('/api/ssh_tunnel/logs/clear', {});
  });

  /* ---------------- 启动 ---------------- */

  schedulePreview();
})();
