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
  var connectionInfo = null;      // /api/tools 的 connection 字段：mode / online / error …
  var toggleMenu = null;          // 左下角切换 ADB / SSH 的上弹菜单
  var toggleMode = null;          // 菜单打开时的模式，模式变了就自动关掉

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

    // 连接方式一行放在最上面：ADB 还是 SSH，一眼可见。
    // 点击 / Enter / 空格 = 弹菜单选；Shift+点击 = 直接去设置页改细节。
    var connRow = buildStatusRow('connection-status', '连接', 'connection-text', '读取中');
    connRow.classList.add('is-link');
    connRow.tabIndex = 0;
    connRow.setAttribute('role', 'button');
    connRow.setAttribute('aria-haspopup', 'menu');
    connRow.setAttribute('aria-expanded', 'false');
    connRow.setAttribute('aria-label', '连接方式，点击选择 ADB / SSH');
    connRow.addEventListener('click', toggleConnection);
    connRow.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        toggleConnection(ev);
      }
    });
    foot.appendChild(connRow);

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

    // 说明页和设置页都不在工具列表里：一个讲「怎么用」，一个定数据存哪
    var help = el('a', 'btn ghost small', '使用说明');
    help.id = 'help';
    help.href = '/help';
    help.title = '每个功能做什么、怎么用、容易踩哪些坑';
    help.setAttribute('aria-label', '使用说明');
    if (activeId === 'help') help.setAttribute('aria-current', 'page');
    actions.appendChild(help);

    var settings = el('a', 'btn ghost small', '设置');
    settings.id = 'settings';
    settings.href = '/settings';
    settings.title = '选择配置、脚本、预设文件和日志保存在哪里';
    settings.setAttribute('aria-label', '设置');
    if (activeId === 'settings') settings.setAttribute('aria-current', 'page');
    actions.appendChild(settings);

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

  function renderConnection() {
    var row = $('#connection-status');
    if (!row) return;
    var text = $('#connection-text');
    var info = connectionInfo;

    var tone = 'connecting';
    var main = '读取中';
    var detail = '';
    if (info && info.mode === 'ssh') {
      var ssh = info.ssh || {};
      main = 'SSH ' + (ssh.user || 'root') + '@' + (ssh.host || '') +
        ':' + (ssh.port || 22);
      if (info.online) {
        tone = 'ok';
        detail = '通道正常' + (info.model ? ' · ' + info.model : '');
      } else {
        tone = 'warn';
        detail = info.error || '还没连上';
      }
    } else if (info) {
      tone = 'idle';
      main = 'ADB';
      detail = '当前走 adb 通道';
    }

    row.setAttribute('data-tone', tone);
    if (text) text.textContent = main;
    row.title = (detail ? detail + '\n' : '') +
      '点击选择 ADB / SSH。\nShift+点击打开设置页，改地址、端口、密钥等细节。';
    row.setAttribute('aria-label', '连接方式：' + main + '，点击选择 ADB / SSH');
  }

  /* ---------------- 连接方式选择菜单 ---------------- */

  function toggleConnection(ev) {
    if (ev && (ev.shiftKey || ev.ctrlKey || ev.metaKey || ev.altKey)) {
      location.href = '/settings#conn-card';
      return;
    }
    if (toggleMenu) {
      closeToggleMenu();
      return;
    }
    openToggleMenu();
  }

  function closeToggleMenu(focusBack) {
    if (!toggleMenu) return;
    var menu = toggleMenu;
    toggleMenu = null;
    toggleMode = null;
    if (menu.parentNode) menu.parentNode.removeChild(menu);
    document.removeEventListener('mousedown', onMenuOutside, true);
    var row = $('#connection-status');
    if (row) {
      row.setAttribute('aria-expanded', 'false');
      row.classList.remove('is-open');
      if (focusBack !== false) row.focus();
    }
  }

  function onMenuOutside(ev) {
    var menu = toggleMenu;
    var row = $('#connection-status');
    if (!menu) return;
    if (menu.contains(ev.target) || (row && row.contains(ev.target))) return;
    closeToggleMenu(false);
  }

  function menuItems() {
    if (!toggleMenu) return [];
    var list = toggleMenu.querySelectorAll('.conn-menu-item');
    return Array.prototype.slice.call(list);
  }

  function onMenuKey(ev) {
    if (!toggleMenu) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      closeToggleMenu();
      return;
    }
    if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp' &&
        ev.key !== 'Home' && ev.key !== 'End') {
      return;
    }
    var list = menuItems();
    if (!list.length) return;
    ev.preventDefault();
    var idx = list.indexOf(document.activeElement);
    if (ev.key === 'Home') idx = 0;
    else if (ev.key === 'End') idx = list.length - 1;
    else if (ev.key === 'ArrowDown') idx = idx < 0 ? 0 : (idx + 1) % list.length;
    else idx = idx <= 0 ? list.length - 1 : idx - 1;
    list[idx].focus();
  }

  function openToggleMenu() {
    var row = $('#connection-status');
    if (!row) return;

    var info = connectionInfo;
    var mode = info ? info.mode : 'adb';
    var ssh = (info && info.ssh) || {};
    var sshReady = !!ssh.host;
    var online = !!(info && info.online);

    var menu = el('div', 'conn-menu');
    menu.id = 'conn-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', '选择连接方式');

    function addItem(name, sub, active, select) {
      var item = el('button', 'conn-menu-item');
      item.type = 'button';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('aria-checked', active ? 'true' : 'false');
      var check = el('span', 'conn-menu-check', active ? '✓' : '');
      check.setAttribute('aria-hidden', 'true');
      item.appendChild(check);
      var body = el('span', 'conn-menu-body');
      body.appendChild(el('span', 'conn-menu-name', name));
      if (sub) body.appendChild(el('span', 'conn-menu-sub', sub));
      item.appendChild(body);
      item.addEventListener('click', select);
      menu.appendChild(item);
      return item;
    }

    addItem('ADB', '本机 adb 通道', mode === 'adb', function () {
      if (mode === 'adb') { closeToggleMenu(); return; }
      switchTo('adb');
    });

    var sshSub;
    if (!sshReady) {
      sshSub = '未配置 · 去设置';
    } else if (online) {
      sshSub = '通道正常' + (info.model ? ' · ' + info.model : '');
    } else {
      sshSub = '连不上' +
        (info.error ? ' · ' + String(info.error).split('\n')[0].slice(0, 28) : '');
    }
    addItem(
      sshReady
        ? 'SSH ' + (ssh.user || 'root') + '@' + ssh.host + ':' + (ssh.port || 22)
        : 'SSH',
      sshSub,
      mode === 'ssh',
      function () {
        if (mode === 'ssh') { closeToggleMenu(); return; }
        if (!sshReady) {
          // 一次都没配过 SSH，先带用户去设置页填地址
          location.href = '/settings#conn-card';
          return;
        }
        switchTo('ssh');
      }
    );

    menu.appendChild(el('div', 'conn-menu-sep'));

    var more = el('button', 'conn-menu-item conn-menu-more', '⚙ SSH 设置…');
    more.type = 'button';
    more.setAttribute('role', 'menuitem');
    more.addEventListener('click', function () {
      location.href = '/settings#conn-card';
    });
    menu.appendChild(more);

    var err = el('p', 'conn-menu-error');
    err.hidden = true;
    menu.appendChild(err);

    document.body.appendChild(menu);
    toggleMenu = menu;
    toggleMode = mode;

    // 固定在连接行上方：侧边栏在左下角，往上弹不会被裁掉
    var rect = row.getBoundingClientRect();
    menu.style.left = Math.max(8, rect.left) + 'px';
    menu.style.bottom = (window.innerHeight - rect.top + 6) + 'px';

    row.setAttribute('aria-expanded', 'true');
    row.classList.add('is-open');
    document.addEventListener('mousedown', onMenuOutside, true);
    menu.addEventListener('keydown', onMenuKey);

    var current = menu.querySelector('[aria-checked="true"]');
    (current || menu.querySelector('.conn-menu-item')).focus();
  }

  function setMenuBusy(busy) {
    if (!toggleMenu) return;
    menuItems().forEach(function (item) { item.disabled = !!busy; });
    toggleMenu.classList.toggle('is-busy', !!busy);
  }

  function showMenuError(msg) {
    if (!toggleMenu) return;
    var err = toggleMenu.querySelector('.conn-menu-error');
    if (!err) return;
    err.textContent = msg;
    err.hidden = false;
  }

  /* 选定后调 /api/connection，成功刷新当前页；失败把原因留在菜单里。 */
  function switchTo(mode) {
    var body = { mode: mode };
    if (mode === 'ssh') body.ssh = (connectionInfo && connectionInfo.ssh) || {};
    setMenuBusy(true);
    ADB.postJSON('/api/connection', body).then(function (res) {
      if (!res || res.ok === false) {
        setMenuBusy(false);
        showMenuError((res && res.error) || '切换失败');
        return;
      }
      // 切到 SSH 后 always-adb 不再适用：回到当前模式的第一个页面
      if (mode === 'ssh' && activeId === 'always_adb') {
        location.href = '/';
        return;
      }
      location.reload();
    }).catch(function () {
      setMenuBusy(false);
      showMenuError('切换失败：后端没响应');
    });
  }

  function renderDevice() {
    var row = $('#dev-status');
    if (!row) return;

    // SSH 模式没有 adb 设备列表：这一行改成通道状态
    if (connectionInfo && connectionInfo.mode === 'ssh') {
      row.hidden = false;
      var info = connectionInfo;
      var sshText = $('#dev-text');
      var sshMain;
      var sshTone;
      if (info.online) {
        sshMain = 'SSH 在线' + (info.model ? ' · ' + info.model : '');
        sshTone = 'ok';
      } else {
        var reason = String(info.error || '').split('\n')[0].slice(0, 40);
        sshMain = '连不上' + (reason ? ' · ' + reason : '');
        sshTone = 'warn';
      }
      row.setAttribute('data-tone', sshTone);
      if (sshText) sshText.textContent = sshMain;
      row.title = 'SSH 设备：' + sshMain +
        '\n来源：连接设置里的 SSH 地址，点击上面的「连接」行去修改。';
      row.setAttribute('aria-label', 'SSH 设备：' + sshMain);
      return;
    }

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
    // 工具状态里的连接信息比 3 秒轮询更新：SSE 一到就换显示
    if (active && active.connection) {
      connectionInfo = active.connection;
      if (toggleMenu && connectionInfo.mode !== toggleMode) closeToggleMenu(false);
      renderConnection();
      renderDevice();
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
      connectionInfo = data.connection || null;
      if (toggleMenu && connectionInfo && connectionInfo.mode !== toggleMode) {
        closeToggleMenu(false);
      }
      renderConnection();
      renderDevice();
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
