/* ADB Tool Studio 前端公共库
 *
 * 一级页面和两个二级页面共用：主题、连接指示、日志台、状态徽标、工具客户端。
 *
 * 约定：所有来自后端的文本（adb 报错原文、ssh 输出、设备型号）一律走 textContent，
 * 绝不拼 innerHTML —— 这些是不可信数据。
 */
window.ADB = (function () {
  'use strict';

  var LEVEL_TEXT = { cmd: '命令', info: '信息', ok: '成功', warn: '警告', err: '失败' };
  var LEVELS = ['cmd', 'info', 'ok', 'warn', 'err'];

  var BADGE = {
    idle: { tone: 'idle', icon: '■', text: '已停止' },
    running: { tone: 'running', icon: '▶', text: '运行中' },
    stopping: { tone: 'warn', icon: '⏸', text: '停止中' },
    reconnecting: { tone: 'warn', icon: '↻', text: '重连中' }
  };

  function $(sel, root) { return (root || document).querySelector(sel); }

  function getJSON(url) {
    return fetch(url).then(function (r) { return r.json(); });
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, message: 'HTTP ' + r.status };
      });
    });
  }

  /* ---------------- 格式化 ---------------- */

  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  function num(n) { return (Number(n) || 0).toLocaleString('zh-CN'); }

  function clockTime(epochSeconds) {
    if (!epochSeconds) return '—';
    var d = new Date(epochSeconds * 1000);
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  function duration(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    if (h > 0) return h + ' 小时 ' + m + ' 分';
    if (m > 0) return m + ' 分 ' + s + ' 秒';
    return s + ' 秒';
  }

  /* ---------------- 主题 ---------------- */

  function systemDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function currentTheme() {
    var attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'dark' || attr === 'light') return attr;
    return systemDark() ? 'dark' : 'light';
  }

  function applyTheme(theme, btn) {
    document.documentElement.setAttribute('data-theme', theme);
    if (btn) btn.textContent = theme === 'dark' ? '浅色' : '深色';
    try { localStorage.setItem('myAdbTheme', theme); } catch (e) { /* 隐私模式 */ }
  }

  function initTheme(btnId) {
    var btn = $(btnId || '#theme');
    if (!btn) return;
    try {
      var saved = localStorage.getItem('myAdbTheme');
      if (saved === 'dark' || saved === 'light') applyTheme(saved, btn);
      else btn.textContent = systemDark() ? '浅色' : '深色';
    } catch (e) {
      btn.textContent = systemDark() ? '浅色' : '深色';
    }
    btn.addEventListener('click', function () {
      applyTheme(currentTheme() === 'dark' ? 'light' : 'dark', btn);
    });
  }

  /* ---------------- 连接指示 ---------------- */

  function setConn(state, text) {
    var el = $('#conn');
    if (!el) return;
    el.setAttribute('data-state', state);
    var t = $('#conn-text');
    if (t) t.textContent = text;
  }

  // 侧边栏的“推送”只是 HTTP 那一行的后缀，不影响 HTTP 主状态，
  // 免得 SSE 断线时把“已连接”反复改回“重连中”。
  function setPush(state) {
    if (window.SHELL && window.SHELL.setPush) window.SHELL.setPush(state);
  }

  // 当前页的 SSE 状态里带着设备列表，顺手喂给侧边栏，设备上线就能立刻看到
  function setShellDevices(data) {
    if (!window.SHELL || !window.SHELL.setDevices) return;
    window.SHELL.setDevices(
      data && data.extra ? data.extra.devices : undefined, true);
  }

  /* ---------------- 状态徽标 ---------------- */

  function paintBadge(el, status, override) {
    if (!el) return;
    var badge = override || BADGE[status] || BADGE.idle;
    el.setAttribute('data-tone', badge.tone);
    var icon = el.querySelector('.badge-icon');
    var text = el.querySelector('.badge-text');
    if (icon) icon.textContent = badge.icon;
    if (text) text.textContent = badge.text;
  }

  /* ---------------- 日志台 ---------------- */

  function LogConsole(el, followEl, options) {
    options = options || {};
    this.el = el;
    this.follow = followEl;
    this.max = options.max || 2000;
    var self = this;
    if (followEl) {
      // 用户自己往上翻时自动取消跟随
      el.addEventListener('scroll', function () {
        var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        if (!atBottom && self.follow.checked) self.follow.checked = false;
      });
    }
  }

  LogConsole.prototype._line = function (entry) {
    var line = document.createElement('div');
    line.className = 'line';
    line.setAttribute('data-level', entry.level);

    var ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = entry.time;

    var tag = document.createElement('span');
    tag.className = 'tag';
    var tagText = document.createElement('span');
    tagText.className = 'tag-text';
    tagText.textContent = LEVEL_TEXT[entry.level] || entry.level;
    tag.appendChild(tagText);

    var msg = document.createElement('span');
    msg.className = 'msg';
    msg.textContent = entry.msg;

    line.appendChild(ts);
    line.appendChild(tag);
    line.appendChild(msg);
    return line;
  };

  LogConsole.prototype._trim = function () {
    while (this.el.childElementCount > this.max) {
      this.el.removeChild(this.el.firstElementChild);
    }
  };

  LogConsole.prototype._scroll = function () {
    if (!this.follow || this.follow.checked) this.el.scrollTop = this.el.scrollHeight;
  };

  LogConsole.prototype.append = function (entry) {
    this.el.appendChild(this._line(entry));
    this._trim();
    this._scroll();
  };

  // 打开页面时一次补几百条历史：攒到一个 fragment 里再挂上，
  // 只做一次 trim / 滚动。逐条 append 的话每一条都会触发一次重排，
  // 日志多的页面（always-adb）切换过来就会卡一下。
  LogConsole.prototype.appendAll = function (entries) {
    if (!entries || !entries.length) return;
    var frag = document.createDocumentFragment();
    for (var i = 0; i < entries.length; i++) {
      frag.appendChild(this._line(entries[i]));
    }
    this.el.appendChild(frag);
    this._trim();
    this._scroll();
  };

  LogConsole.prototype.clear = function () { this.el.textContent = ''; };

  /* ---------------- 工具客户端 ---------------- */

  function ToolClient(id) {
    this.id = id;
    this.state = null;
    this._state = null;
    this._log = null;
  }

  ToolClient.prototype.onState = function (fn) { this._state = fn; return this; };
  ToolClient.prototype.onLog = function (fn) { this._log = fn; return this; };

  ToolClient.prototype.connect = function (onSnapshot) {
    var self = this;
    var es = new EventSource('/api/' + this.id + '/events');

    es.addEventListener('open', function () { setPush('open'); });

    es.addEventListener('snapshot', function (ev) {
      var data = JSON.parse(ev.data);
      self.state = data;
      setShellDevices(data);
      if (onSnapshot) onSnapshot(data);
      if (self._state) self._state(data);
    });

    es.addEventListener('state', function (ev) {
      self.state = JSON.parse(ev.data);
      setShellDevices(self.state);
      if (self._state) self._state(self.state);
    });

    es.addEventListener('log', function (ev) {
      if (self._log) self._log(JSON.parse(ev.data));
    });

    // EventSource 自己会重连，这里只反映“推送”状态，不影响 HTTP 主状态
    es.onerror = function () { setPush('retry'); };
    this.source = es;
    return es;
  };

  ToolClient.prototype.post = function (action, body) {
    var self = this;
    return postJSON('/api/' + this.id + '/' + action, body).then(function (data) {
      if (data && data.state) {
        self.state = data.state;
        if (self._state) self._state(data.state);
      }
      return data;
    });
  };

  ToolClient.prototype.start = function (options) { return this.post('start', options || {}); };
  ToolClient.prototype.stop = function () { return this.post('stop', {}); };
  ToolClient.prototype.saveConfig = function (cfg) { return this.post('config', cfg); };
  ToolClient.prototype.preview = function (params) { return this.post('preview', params); };
  ToolClient.prototype.devices = function () { return getJSON('/api/' + this.id + '/devices'); };

  /* ---------------- 小工具 ---------------- */

  // 表单里取数字，空/非法时回落到默认值
  function numberValue(sel, fallback) {
    var v = parseFloat($(sel).value);
    return isNaN(v) ? fallback : v;
  }

  function copyText(text, statusEl, okText) {
    function done() {
      if (!statusEl) return;
      statusEl.removeAttribute('data-tone');
      statusEl.textContent = okText || '已复制';
      setTimeout(function () { statusEl.textContent = ''; }, 2000);
    }
    function fail() {
      if (!statusEl) return;
      statusEl.setAttribute('data-tone', 'error');
      statusEl.textContent = '复制失败，请手动选中';
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fail);
      return;
    }
    // 非 https / 老浏览器没有 clipboard API，退回 execCommand
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done();
    } catch (e) {
      fail();
    }
  }

  return {
    $: $,
    getJSON: getJSON,
    postJSON: postJSON,
    levelText: LEVEL_TEXT,
    levels: LEVELS,
    badge: BADGE,
    num: num,
    pad: pad,
    clockTime: clockTime,
    duration: duration,
    initTheme: initTheme,
    setConn: setConn,
    paintBadge: paintBadge,
    LogConsole: LogConsole,
    ToolClient: ToolClient,
    numberValue: numberValue,
    copyText: copyText
  };
})();
