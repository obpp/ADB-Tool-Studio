/* 全局连接方式（ADB / SSH）
 *
 * 设置页放卡片；各功能页用 CONNECTION.applyToolPage() 把 ADB 专属的字段
 * 收起来，换成一枚“SSH · user@host”的提示。缓存的是后端 /api/connection
 * 返回的 connection 对象（mode / online / deviceCount / model / error …）。
 *
 * 约定同 common.js：来自后端的文本一律 textContent。
 */
window.CONNECTION = (function () {
  'use strict';

  var cached = null;
  var dirtyMode = null;      // 卡片里尚未保存的模式选择；null 表示跟随已保存的

  function $(sel) { return document.querySelector(sel); }

  function setText(sel, text) {
    var el = $(sel);
    if (el) el.textContent = text;
  }

  function setTone(sel, tone) {
    var el = $(sel);
    if (!el) return;
    if (tone) el.setAttribute('data-tone', tone);
    else el.removeAttribute('data-tone');
  }

  function isSsh(info) {
    info = info || cached;
    return !!(info && info.mode === 'ssh');
  }

  function targetText(info) {
    info = info || cached;
    if (!isSsh(info)) return '';
    var ssh = info.ssh || {};
    return (ssh.user || 'root') + '@' + (ssh.host || '');
  }

  /* ---------------- 各功能页 ---------------- */

  function applyToolPage(info) {
    info = info || cached;
    if (!info) return;
    var ssh = isSsh(info);
    var adbField = $('#adb-path-field');
    var deviceField = $('#device-field');
    var sshField = $('#ssh-target-field');
    if (adbField) adbField.hidden = ssh;
    if (deviceField) deviceField.hidden = ssh;
    if (sshField) {
      sshField.hidden = !ssh;
      sshField.setAttribute('data-tone', info.online ? 'ok' : 'warn');
    }
    if (ssh) {
      var sshCfg = info.ssh || {};
      setText('#ssh-target-text',
              'SSH · ' + (sshCfg.user || 'root') + ':' +
              (sshCfg.host || '') + ':' + (sshCfg.port || 22));
    }
  }

  /* ---------------- 设置卡片 ---------------- */

  function setModeButtons(mode) {
    var isSshMode = mode === 'ssh';
    var adbBtn = $('#conn-mode-adb');
    var sshBtn = $('#conn-mode-ssh');
    if (adbBtn) {
      adbBtn.classList.toggle('is-active', !isSshMode);
      adbBtn.setAttribute('aria-selected', isSshMode ? 'false' : 'true');
    }
    if (sshBtn) {
      sshBtn.classList.toggle('is-active', isSshMode);
      sshBtn.setAttribute('aria-selected', isSshMode ? 'true' : 'false');
    }
    var adbPanel = $('#conn-panel-adb');
    var sshPanel = $('#conn-panel-ssh');
    if (adbPanel) adbPanel.hidden = isSshMode;
    if (sshPanel) sshPanel.hidden = !isSshMode;
    var testBtn = $('#btn-conn-test');
    if (testBtn) testBtn.disabled = !isSshMode;
  }

  function fillSsh(ssh) {
    ssh = ssh || {};
    var host = $('#conn-host');
    var port = $('#conn-port');
    var user = $('#conn-user');
    var key = $('#conn-key');
    var policy = $('#conn-policy');
    var timeout = $('#conn-timeout');
    var remount = $('#conn-remount');
    if (host) host.value = ssh.host || '';
    if (port) port.value = ssh.port || 22;
    if (user) user.value = ssh.user || 'root';
    if (key) key.value = ssh.keyFile || '';
    if (policy) policy.value = ssh.hostKeyPolicy || 'accept-new';
    if (timeout) timeout.value = ssh.connectTimeout || 10;
    if (remount) remount.value = ssh.remountCommand || '';
  }

  function collectSsh() {
    function val(sel) { var el = $(sel); return el ? el.value : ''; }
    var port = parseInt(val('#conn-port'), 10);
    var timeout = parseInt(val('#conn-timeout'), 10);
    return {
      host: val('#conn-host').trim(),
      port: isNaN(port) ? 22 : port,
      user: val('#conn-user').trim() || 'root',
      keyFile: val('#conn-key').trim(),
      hostKeyPolicy: val('#conn-policy') || 'accept-new',
      connectTimeout: isNaN(timeout) ? 10 : timeout,
      remountCommand: val('#conn-remount').trim()
    };
  }

  function paintStatus(info) {
    var status = $('#conn-status');
    var running = $('#conn-running');
    if (!status) return;
    if (running) {
      var names = info && info.running;
      running.hidden = !(names && names.length);
    }
    setTone('#conn-status', null);
    if (isSsh(info)) {
      var ssh = info.ssh || {};
      if (info.online) {
        setTone('#conn-status', 'ok');
        status.textContent = (ssh.user || 'root') + '@' + (ssh.host || '') +
          (info.model ? ' · ' + info.model : '');
      } else {
        setTone('#conn-status', 'error');
        status.textContent = info.error || '还没测试连接';
      }
      return;
    }
    var n = (info && info.deviceCount) || 0;
    status.textContent = n > 0
      ? 'ADB · ' + n + ' 台设备在线'
      : 'ADB · 没有检测到在线设备';
  }

  function paintCard() {
    if (!$('#conn-card')) return;
    var mode = dirtyMode || (cached && cached.mode) || 'adb';
    setModeButtons(mode);
    if (cached) fillSsh(cached.ssh);
    paintStatus(cached || {});
    var saveBtn = $('#btn-conn-save');
    var running = $('#conn-running');
    if (saveBtn) {
      var busy = !!(cached && cached.running && cached.running.length);
      saveBtn.disabled = busy;
      if (running && busy) {
        running.textContent = cached.running.join('、') +
          ' 正在运行，先全部停止再切换';
      }
    }
  }

  function paintPage() {
    paintCard();
    applyToolPage(cached);
  }

  function load() {
    return ADB.getJSON('/api/connection').then(function (data) {
      if (data && data.connection) {
        cached = data.connection;
        if (data.running) cached.running = data.running;
        if (data.config && data.config.ssh && !cached.ssh) {
          cached.ssh = data.config.ssh;
        }
      }
      paintPage();
      return cached;
    });
  }

  function update(info) {
    cached = info || null;
    paintPage();
  }

  function setStatus(text, tone) {
    var status = $('#conn-status');
    if (!status) return;
    status.textContent = text;
    if (tone) status.setAttribute('data-tone', tone);
    else status.removeAttribute('data-tone');
  }

  function bindCard() {
    if (!$('#conn-card')) return;

    function choose(mode) {
      dirtyMode = mode;
      setModeButtons(mode);
    }
    var adbBtn = $('#conn-mode-adb');
    var sshBtn = $('#conn-mode-ssh');
    if (adbBtn) adbBtn.addEventListener('click', function () { choose('adb'); });
    if (sshBtn) sshBtn.addEventListener('click', function () { choose('ssh'); });

    function browse() {
      var api = window.pywebview && window.pywebview.api;
      if (api && api.pick_files) {
        api.pick_files('').then(function (paths) {
          if (paths && paths.length) $('#conn-key').value = paths[0];
        }, function () {});
        return;
      }
      var input = $('#conn-key');
      if (input) {
        input.focus();
        setStatus('网页版拿不到本地路径，请手动填写密钥文件', 'error');
      }
    }
    var keyBtn = $('#btn-conn-key');
    if (keyBtn) keyBtn.addEventListener('click', browse);

    var testBtn = $('#btn-conn-test');
    if (testBtn) {
      testBtn.addEventListener('click', function () {
        testBtn.disabled = true;
        setStatus('正在测试连接…', null);
        ADB.postJSON('/api/connection/test', { ssh: collectSsh() })
          .then(function (res) {
            if (res && res.ok) {
              setStatus((res.model || 'SSH') + ' · 连接正常', 'ok');
            } else {
              setStatus((res && res.error) || '连不上', 'error');
            }
          }, function () {
            setStatus('测试失败：后端没有响应', 'error');
          })
          .then(function () { testBtn.disabled = false; });
      });
    }

    var saveBtn = $('#btn-conn-save');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        saveBtn.disabled = true;
        setStatus('正在保存…', null);
        ADB.postJSON('/api/connection', {
          mode: dirtyMode || (cached && cached.mode) || 'adb',
          ssh: collectSsh()
        }).then(function (res) {
          if (!res || !res.ok) {
            setStatus((res && (res.error || res.message)) || '保存失败', 'error');
            saveBtn.disabled = false;
            return;
          }
          dirtyMode = null;
          update(res.connection);
          setStatus('已保存', 'ok');
          if (window.SHELL && window.SHELL.refresh) window.SHELL.refresh();
        }, function () {
          setStatus('保存失败：后端没有响应', 'error');
          saveBtn.disabled = false;
        });
      });
    }

    paintCard();
  }

  bindCard();
  load().catch(function () { /* 设置页其他部分还能用 */ });

  return {
    info: function () { return cached; },
    load: load,
    update: update,
    isSsh: function () { return isSsh(cached); },
    targetText: function () { return targetText(cached); },
    applyToolPage: applyToolPage
  };
})();
