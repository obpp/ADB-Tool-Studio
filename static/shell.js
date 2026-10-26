/* ADB Tool Studio —— 页面外壳（侧边栏）
 *
 * 侧边栏不是写死在 HTML 里的：它从 /api/tools 拿工具列表自己画。
 * 所以以后在 tools/__init__.py 里加一个工具，侧边栏自动多一行，不用改前端。
 *
 * 每个通道条目显示：指示灯 + 名称 + 状态读数（状态词 · 已运行多久）。
 * 主体区域的页面通过 <body data-tool="..."> 告诉这里当前选的是哪一个。
 */
window.SHELL = (function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var activeId = document.body.getAttribute('data-tool') || '';
  var items = {};          // tool id -> { el, lamp, meta }
  var running = {};        // tool id -> bool
  var built = false;
  var POLL_MS = 3000;

  // 底部两个状态：HTTP 是页面到本地后端的连接，设备是当前工具看到的 adb 设备。
  // 两者互不代表，显示上分开，颜色和文字同时表意。
  var httpState = 'connecting';   // connecting | open | retry
  var pushState = 'connecting';   // connecting | open | retry（当前页的 SSE 长连接）
  var deviceList = undefined;     // undefined = 当前工具不涉及设备（隐藏这一行）
  var deviceToolName = '';
  var lastDevicePush = 0;         // SSE 推设备列表的时间，防止旧轮询结果把它盖回去

  var DEVICE_WORD = {
    unauthorized: '待授权',
    offline: '离线',
    connecting: '连接中',
    recovery: 'recovery',
    sideload: 'sideload',
    bootloader: 'bootloader'
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function shortSerial(serial) {
    serial = serial || '';
    return serial.length > 16 ? serial.slice(0, 15) + '…' : serial;
  }

  /* ---------------- 画侧边栏 ---------------- */

  function buildSidebar() {
    var aside = $('#sidebar');
    if (!aside) return;

    aside.textContent = '';

    var brand = el('div', 'brand');
    var brandLink = el('a', 'brand-name', 'ADB Tool Studio');
    brandLink.href = '/';
    brand.appendChild(brandLink);
    aside.appendChild(brand);

    aside.appendChild(el('nav', 'chan-nav')).id = 'chan-nav';

    var foot = el('div', 'sidebar-foot');

    foot.appendChild(buildStatusRow('conn', '服务', 'conn-text', '正在连接后端'));
    var dev = buildStatusRow('dev-status', '设备', 'dev-text', '等待设备列表');
    dev.setAttribute('data-tone', 'idle');
    dev.hidden = true;                    // 当前工具不涉及设备时不占位
    foot.appendChild(dev);

    var actions = el('div', 'sidebar-actions');
    var stopAll = el('button', 'btn ghost small', '全部停止');
    stopAll.type = 'button';
    stopAll.id = 'stop-all';
    stopAll.title = '停止所有正在运行的功能';
    stopAll.setAttribute('aria-label', '全部停止');
    foot.appendChild(actions);
    actions.appendChild(stopAll);

    var theme = el('button', 'btn ghost small', '深色');
    theme.type = 'button';
    theme.id = 'theme';
    theme.setAttribute('aria-label', '切换浅色 / 深色主题');
    actions.appendChild(theme);

    aside.appendChild(foot);

    stopAll.addEventListener('click', function () {
      var ids = Object.keys(running).filter(function (id) { return running[id]; });
      if (!ids.length) return;
      stopAll.disabled = true;
      Promise.all(ids.map(function (id) {
        return ADB.postJSON('/api/' + id + '/stop', {});
      })).then(refresh, refresh).then(function () { stopAll.disabled = false; });
    });
  }

  /* ---------------- 底部状态 ---------------- */

  function buildStatusRow(id, label, textId, initialText) {
    var row = el('span', 'conn');
    row.id = id;
    var dot = el('span', 'conn-dot');
    dot.setAttribute('aria-hidden', 'true');
    row.appendChild(dot);
    row.appendChild(el('span', 'conn-label', label));
    var text = el('span', 'conn-text', initialText);
    text.id = textId;
    row.appendChild(text);
    return row;
  }

  function renderConn() {
    var row = $('#conn');
    if (!row) return;

    var main;
    var state = httpState;
    var host = location.host || '本机后端';
    if (httpState === 'open') {
      main = pushState === 'retry'
        ? '已连接 · 推送重连中'
        : '已连接 · ' + host;
    } else if (httpState === 'retry') {
      main = '后端没响应';
    } else {
      main = '加载中…';
      state = 'connecting';
    }

    var pushWord = pushState === 'retry' ? '重连中'
      : (pushState === 'open' ? '正常' : '连接中');

    ADB.setConn(state, main);
    row.title = '后端服务：' + main +
      '\n地址：' + host +
      '\n检测：每 3 秒请求 /api/tools，失败自动重试。' +
      '\n推送：' + pushWord + '（当前页的 SSE 长连接，设备、日志、任务变化即时到达）。' +
      '\n注意：这个状态只表示页面和后端之间通不通，不代表 adb 设备或 adb server 的状态。';
    row.setAttribute('aria-label', '后端服务：' + main + '，推送' + pushWord);
  }

  function renderDevice() {
    var row = $('#dev-status');
    if (!row) return;

    if (deviceList === undefined) {
      row.hidden = true;
      return;
    }
    row.hidden = false;

    var devices = deviceList || [];
    var online = [];
    var bad = [];
    devices.forEach(function (d) {
      if (d.state === 'device') online.push(d);
      else bad.push(d);
    });

    var tone = 'idle';
    var main;
    if (online.length === 1) {
      main = '1 台在线 · ' + shortSerial(online[0].serial);
      tone = 'ok';
    } else if (online.length > 1) {
      main = online.length + ' 台在线 · 未选择';
      tone = 'ok';
    } else if (bad.length === 1) {
      main = '1 台' + (DEVICE_WORD[bad[0].state] || bad[0].state) +
        ' · ' + shortSerial(bad[0].serial);
      tone = 'warn';
    } else if (bad.length > 1) {
      main = bad.length + ' 台未就绪 · 检查授权';
      tone = 'warn';
    } else {
      main = '无在线设备 · 等待接入';
    }

    row.setAttribute('data-tone', tone);
    var text = $('#dev-text');
    if (text) text.textContent = main;

    var lines = [];
    if (online.length) {
      lines.push('在线：' + online.map(function (d) {
        return d.serial + (d.model ? '（' + d.model + '）' : '');
      }).join('、'));
    }
    if (bad.length) {
      lines.push('未就绪：' + bad.map(function (d) {
        return d.serial + '（' + (DEVICE_WORD[d.state] || d.state) + '）';
      }).join('、'));
    }
    if (!lines.length) lines.push('当前没有检测到设备');

    row.title = 'ADB 设备：' + main +
      '\n来源：' + (deviceToolName || '当前页面') + '的设备列表。' +
      '\n' + lines.join('\n') +
      '\n检测：后端常驻 adb wait-for-device / wait-for-disconnect，' +
      '设备上线、掉线会即时推送，不依赖轮询。' +
      '\n注意：这个状态不代表 Web 后端连接状态。';
    row.setAttribute('aria-label', 'ADB 设备：' + main);
  }

  function setPush(state) {
    pushState = state === 'retry' ? 'retry' : 'open';
    renderConn();
  }

  function setDevices(list, fromPush) {
    // undefined = 当前工具不涉及设备，隐藏整行
    if (fromPush) lastDevicePush = Date.now();
    deviceList = (list === undefined || list === null) ? undefined : list;
    renderDevice();
  }

  /* ---------------- 通道条目 ---------------- */

  function buildChannels(tools) {
    var nav = $('#chan-nav');
    if (!nav) return;
    nav.textContent = '';
    items = {};

    tools.forEach(function (tool) {
      var link = el('a', 'chan');
      link.href = '/' + tool.page;
      link.title = tool.name + ' · ' + tool.desc;
      if (tool.id === activeId) {
        link.classList.add('is-active');
        link.setAttribute('aria-current', 'page');
      }

      var lamp = el('span', 'lamp');
      lamp.setAttribute('data-tone', 'idle');

      var body = el('span', 'chan-body');
      body.appendChild(el('span', 'chan-name', tool.name));
      var meta = el('span', 'chan-meta', '—');
      body.appendChild(meta);

      var glyph = el('span', 'chan-glyph', tool.icon || tool.name.slice(0, 1));
      glyph.setAttribute('aria-hidden', 'true');

      link.appendChild(lamp);
      link.appendChild(glyph);
      link.appendChild(body);
      nav.appendChild(link);

      items[tool.id] = { link: link, lamp: lamp, meta: meta };
    });
  }

  /* ---------------- 状态读数 ---------------- */

  function toneFor(tool) {
    if (tool.status === 'idle') {
      var fails = tool.stats && tool.stats.consecutiveFailures;
      return fails > 0 ? 'error' : 'idle';
    }
    if (tool.status === 'running') return 'running';
    return 'warn';
  }

  function readoutFor(tool) {
    var badge = ADB.badge[tool.status] || ADB.badge.idle;
    var fails = tool.stats && tool.stats.consecutiveFailures;
    if (tool.status === 'idle' && fails > 0) return '已停止 · 上次失败';

    var text = badge.text;
    if (tool.status !== 'idle' && tool.uptime > 0) {
      text += ' · ' + ADB.duration(tool.uptime);
    }
    return text;
  }

  function paint(tools) {
    running = {};

    // 当前功能页标题下面的一句话说明，内容来自后端的 Tool.desc
    var descEl = $('#tool-desc');
    var active = null;
    for (var i = 0; i < tools.length; i++) {
      if (tools[i].id === activeId) { active = tools[i]; break; }
    }
    if (descEl) {
      var text = active && active.desc ? active.desc : '';
      if (descEl.textContent !== text) descEl.textContent = text;
    }
    if (active) {
      deviceToolName = active.name || '';
      var extra = active.extra || {};
      // extra 里没有 devices 键 = 这个工具不涉及 adb 设备（比如 ssh 隧道）
      if (!Object.prototype.hasOwnProperty.call(extra, 'devices')) {
        setDevices(undefined);
      } else if (Date.now() - lastDevicePush > POLL_MS) {
        // 刚被 SSE 推过就别用这份可能更旧的轮询结果覆盖
        setDevices(extra.devices);
      }
    }

    tools.forEach(function (tool) {
      var it = items[tool.id];
      if (!it) return;
      it.lamp.setAttribute('data-tone', toneFor(tool));
      it.meta.textContent = readoutFor(tool);
      it.link.title = tool.name + '\n' + readoutFor(tool) +
        (tool.brief ? '\n' + tool.brief : '');
      running[tool.id] = tool.status !== 'idle';
    });
  }

  /* ---------------- 轮询 ---------------- */

  function refresh() {
    return ADB.getJSON('/api/tools').then(function (data) {
      var tools = data.tools || [];
      if (!built) {
        buildChannels(tools);
        built = true;
      }
      paint(tools);
      httpState = 'open';
      renderConn();
    }).catch(function () {
      httpState = 'retry';
      renderConn();
    });
  }

  /* ---------------- 起来 ---------------- */

  buildSidebar();
  ADB.initTheme('#theme');
  renderConn();
  refresh();
  setInterval(refresh, POLL_MS);

  return {
    refresh: refresh,
    setPush: setPush,
    setDevices: setDevices
  };
})();
