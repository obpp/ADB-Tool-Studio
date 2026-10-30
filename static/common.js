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

  // 过滤选择存的是「哪些级别被隐藏」，换页面 / 重开窗口都沿用；
  // 以后新增级别时它不在列表里，默认就是显示。
  var LOG_FILTER_KEY = 'ats-log-filter';

  function setCount(el, n) {
    var text = String(n);
    if (el && el.textContent !== text) el.textContent = text;
  }

  function LogConsole(el, followEl, options) {
    options = options || {};
    this.el = el;
    this.follow = followEl;
    this.max = options.max || 2000;
    this._counts = { cmd: 0, info: 0, ok: 0, warn: 0, err: 0 };
    this._total = 0;        // 缓冲区里的总条数
    this._lines = 0;        // DOM 里的 .line 数，_trim 用
    this._hidden = {};      // level -> true 表示这一类型不显示
    this._chips = {};
    this._allChip = null;
    this._hint = null;
    this._loadFilters();
    this._buildFilters();
    var self = this;
    if (followEl) {
      // 用户自己往上翻时自动取消跟随
      el.addEventListener('scroll', function () {
        var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        if (!atBottom && self.follow.checked) self.follow.checked = false;
      });
    }
  }

  LogConsole.prototype._loadFilters = function () {
    var hidden = null;
    try {
      var saved = JSON.parse(localStorage.getItem(LOG_FILTER_KEY) || 'null');
      if (Object.prototype.toString.call(saved) === '[object Array]') {
        hidden = {};
        for (var i = 0; i < saved.length; i++) hidden[saved[i]] = true;
      }
    } catch (e) { /* 隐私模式或坏数据：按全部显示处理 */ }
    for (var j = 0; j < LEVELS.length; j++) {
      this._hidden[LEVELS[j]] = !!(hidden && hidden[LEVELS[j]]);
    }
  };

  LogConsole.prototype._saveFilters = function () {
    var hidden = [];
    for (var i = 0; i < LEVELS.length; i++) {
      if (this._hidden[LEVELS[i]]) hidden.push(LEVELS[i]);
    }
    try { localStorage.setItem(LOG_FILTER_KEY, JSON.stringify(hidden)); } catch (e) {}
  };

  LogConsole.prototype._chip = function (label, level) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'log-chip' + (level ? '' : ' log-chip-all');
    btn.setAttribute('aria-pressed', 'true');
    btn.setAttribute('data-level', level || '');
    btn.title = level ? '显示 / 隐藏' + label + '日志' : '显示全部类型';
    var name = document.createElement('span');
    name.className = 'log-chip-name';
    name.textContent = label;
    var n = document.createElement('span');
    n.className = 'n';
    n.textContent = '0';
    btn.appendChild(name);
    btn.appendChild(n);
    return btn;
  };

  LogConsole.prototype._buildFilters = function () {
    var self = this;
    var bar = document.createElement('div');
    bar.className = 'log-filters';
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', '日志类型过滤');

    this._allChip = this._chip('全部', null);
    this._allChip.addEventListener('click', function () {
      for (var i = 0; i < LEVELS.length; i++) self._hidden[LEVELS[i]] = false;
      self._saveFilters();
      self._applyFilters();
    });
    bar.appendChild(this._allChip);

    var sep = document.createElement('span');
    sep.className = 'sep';
    sep.setAttribute('aria-hidden', 'true');
    bar.appendChild(sep);

    for (var i = 0; i < LEVELS.length; i++) {
      (function (level) {
        var chip = self._chip(LEVEL_TEXT[level], level);
        chip.addEventListener('click', function () {
          self._hidden[level] = !self._hidden[level];
          self._saveFilters();
          self._applyFilters();
        });
        bar.appendChild(chip);
        self._chips[level] = chip;
      })(LEVELS[i]);
    }

    if (this.el.parentNode) this.el.parentNode.insertBefore(bar, this.el);
    this._refreshChips();
  };

  LogConsole.prototype._visible = function () {
    var n = 0;
    for (var i = 0; i < LEVELS.length; i++) {
      if (!this._hidden[LEVELS[i]]) n += this._counts[LEVELS[i]] || 0;
    }
    return n;
  };

  LogConsole.prototype._refreshChips = function () {
    var allOn = true;
    for (var i = 0; i < LEVELS.length; i++) {
      var level = LEVELS[i];
      var on = !this._hidden[level];
      if (!on) allOn = false;
      var chip = this._chips[level];
      if (!chip) continue;
      chip.setAttribute('aria-pressed', on ? 'true' : 'false');
      setCount(chip.querySelector('.n'), this._counts[level] || 0);
    }
    if (this._allChip) {
      this._allChip.setAttribute('aria-pressed', allOn ? 'true' : 'false');
      this._allChip.querySelector('.log-chip-name').textContent =
        allOn ? '全部' : '全选';
      setCount(this._allChip.querySelector('.n'), this._total);
    }
  };

  // 过滤只切 display，不删行：历史日志还在缓冲区里，点回来马上就能看见。
  LogConsole.prototype._applyFilters = function () {
    var children = this.el.children;
    for (var i = 0; i < children.length; i++) {
      var line = children[i];
      if (!line.classList || !line.classList.contains('line')) continue;
      line.classList.toggle('is-hidden',
                            !!this._hidden[line.getAttribute('data-level')]);
    }
    this._refreshChips();
    this._refreshEmpty();
    this._scroll();
  };

  LogConsole.prototype._refreshEmpty = function () {
    var show = this._total > 0 && this._visible() === 0;
    if (show) {
      if (!this._hint) {
        this._hint = document.createElement('div');
        this._hint.className = 'log-hint';
        this.el.appendChild(this._hint);
      }
      this._hint.textContent =
        '没有符合当前过滤条件的日志（共 ' + this._total + ' 条）';
    } else if (this._hint) {
      this.el.removeChild(this._hint);
      this._hint = null;
    }
  };

  LogConsole.prototype._line = function (entry) {
    var level = entry.level;
    var line = document.createElement('div');
    line.className = 'line' + (this._hidden[level] ? ' is-hidden' : '');
    line.setAttribute('data-level', level);

    var ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = entry.time;

    var tag = document.createElement('span');
    tag.className = 'tag';
    var tagText = document.createElement('span');
    tagText.className = 'tag-text';
    tagText.textContent = LEVEL_TEXT[level] || level;
    tag.appendChild(tagText);

    var msg = document.createElement('span');
    msg.className = 'msg';
    msg.textContent = entry.msg;

    line.appendChild(ts);
    line.appendChild(tag);
    line.appendChild(msg);

    this._counts[level] = (this._counts[level] || 0) + 1;
    this._total++;
    this._lines++;
    return line;
  };

  LogConsole.prototype._trim = function () {
    while (this._lines > this.max) {
      var first = this.el.firstElementChild;
      if (!first) break;
      if (first.classList && first.classList.contains('line')) {
        var level = first.getAttribute('data-level');
        if (this._counts[level]) this._counts[level]--;
        this._total--;
        this._lines--;
      }
      this.el.removeChild(first);
    }
  };

  LogConsole.prototype._scroll = function () {
    if (!this.follow || this.follow.checked) this.el.scrollTop = this.el.scrollHeight;
  };

  LogConsole.prototype.append = function (entry) {
    this.el.appendChild(this._line(entry));
    this._trim();
    this._scroll();
    this._refreshChips();
    this._refreshEmpty();
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
    this._refreshChips();
    this._refreshEmpty();
  };

  LogConsole.prototype.clear = function () {
    this.el.textContent = '';
    this._lines = 0;
    this._total = 0;
    this._hint = null;
    for (var i = 0; i < LEVELS.length; i++) this._counts[LEVELS[i]] = 0;
    this._refreshChips();
  };

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

  /* ---------------- 页面存活探针 / 错误上报 ----------------
   * 桌面版把心跳和 JS 报错都写进 app.log。白屏时页面本身没东西可看，
   * 至少后端能留下「最后一次心跳是什么时候、之前报了什么错」。
   * 这里必须用原生 fetch，而且自己兜住异常 —— 上报通路不能反过来把页面搞崩。
   */
  var reportCount = 0;
  var reportSeen = {};

  function clientPage() {
    try { return location.pathname || '/'; } catch (e) { return '/'; }
  }

  // 同一个标签页里换页要算同一个页面，所以 id 放 sessionStorage：
  // 页面导航、刷新都沿用；关掉标签页/重开窗口才会换新的。
  function clientId() {
    try {
      var id = sessionStorage.getItem('ats-ui-id');
      if (!id) {
        id = 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
        sessionStorage.setItem('ats-ui-id', id);
      }
      return id;
    } catch (e) {
      return 'anonymous';
    }
  }

  var uiId = clientId();

  function clientLog(level, message) {
    if (reportCount >= 20) return;          // 页面真炸了也只报前 20 条，别刷爆日志
    message = String(message || '').slice(0, 1000);
    if (!message) return;
    var now = Date.now();
    var key = level + '|' + message;
    if (reportSeen[key] && now - reportSeen[key] < 10000) return;
    reportSeen[key] = now;
    reportCount++;
    try {
      fetch('/api/client/log', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          level: level, message: message, page: clientPage()
        }),
        keepalive: true
      }).catch(function () {});
    } catch (e) { /* 后端没起来时静默 */ }
  }

  function heartbeat() {
    try {
      fetch('/api/client/heartbeat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: uiId,
          page: clientPage(),
          visible: !document.hidden
        })
      }).catch(function () {});
    } catch (e) { /* 同上 */ }
  }

  function describeError(ev) {
    if (ev && ev.message) {
      var where = (ev.filename || '') + ':' +
        (ev.lineno || 0) + ':' + (ev.colno || 0);
      return ev.message + ' @ ' + where +
        (ev.error && ev.error.stack ? ' | ' + ev.error.stack : '');
    }
    var target = ev && ev.target;
    if (target && target !== window) {
      return '资源加载失败: ' +
        (target.src || target.href || target.tagName || '未知');
    }
    return '未知错误';
  }

  // 资源加载错误（<script>/<link>）不冒泡，必须用捕获阶段
  window.addEventListener('error', function (ev) {
    clientLog('error', describeError(ev));
  }, true);

  window.addEventListener('unhandledrejection', function (ev) {
    var reason = ev && ev.reason;
    var text;
    if (reason && reason.stack) text = reason.stack;
    else if (reason && reason.message) text = reason.message;
    else text = String(reason);
    clientLog('reject', '未处理的 Promise 拒绝: ' + text);
  });

  heartbeat();
  setInterval(heartbeat, 10000);
  document.addEventListener('visibilitychange', heartbeat);

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
