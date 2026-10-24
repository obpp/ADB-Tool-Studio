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

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
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

    var conn = el('span', 'conn');
    conn.id = 'conn';
    conn.setAttribute('data-state', 'connecting');
    var dot = el('span', 'conn-dot');
    dot.setAttribute('aria-hidden', 'true');
    conn.appendChild(dot);
    var connText = el('span', 'conn-text', '加载中…');
    connText.id = 'conn-text';
    conn.appendChild(connText);
    foot.appendChild(conn);

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
    if (descEl) {
      var active = null;
      for (var i = 0; i < tools.length; i++) {
        if (tools[i].id === activeId) { active = tools[i]; break; }
      }
      var text = active && active.desc ? active.desc : '';
      if (descEl.textContent !== text) descEl.textContent = text;
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
      ADB.setConn('open', '已连接');
    }).catch(function () {
      ADB.setConn('retry', '后端没响应');
    });
  }

  /* ---------------- 起来 ---------------- */

  buildSidebar();
  ADB.initTheme('#theme');
  refresh();
  setInterval(refresh, POLL_MS);

  return { refresh: refresh };
})();
