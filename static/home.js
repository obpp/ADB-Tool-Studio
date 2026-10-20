/* 一级页面：功能列表 + 快速启停 */
(function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var cards = {};        // tool id -> 卡片里要更新的元素
  var running = {};      // tool id -> bool
  var built = false;

  function span(cls, text) {
    var el = document.createElement('span');
    el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function buildCard(tool) {
    var card = document.createElement('article');
    card.className = 'tool-card';

    var head = document.createElement('div');
    head.className = 'tool-head';
    head.appendChild(span('tool-glyph', tool.icon || '•'));

    var h3 = document.createElement('h3');
    h3.textContent = tool.name;
    head.appendChild(h3);

    var badge = document.createElement('span');
    badge.className = 'badge small';
    badge.appendChild(span('badge-icon'));
    badge.appendChild(span('badge-text'));
    head.appendChild(badge);

    var desc = document.createElement('p');
    desc.className = 'tool-desc';
    desc.textContent = tool.desc;

    var brief = document.createElement('p');
    brief.className = 'tool-brief';

    var actions = document.createElement('div');
    actions.className = 'tool-actions';

    var open = document.createElement('a');
    open.className = 'btn primary';
    open.href = '/' + tool.page;
    open.textContent = '打开';

    var startBtn = document.createElement('button');
    startBtn.type = 'button';
    startBtn.className = 'btn';
    startBtn.textContent = '启动';

    var stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'btn danger';
    stopBtn.textContent = '停止';

    startBtn.addEventListener('click', function () { act(tool.id, 'start', startBtn); });
    stopBtn.addEventListener('click', function () { act(tool.id, 'stop', stopBtn); });

    actions.appendChild(open);
    actions.appendChild(startBtn);
    actions.appendChild(stopBtn);

    card.appendChild(head);
    card.appendChild(desc);
    card.appendChild(brief);
    card.appendChild(actions);

    return {
      el: card, badge: badge, brief: brief,
      start: startBtn, stop: stopBtn
    };
  }

  function render(list) {
    var host = $('#tools');

    if (!built) {
      host.textContent = '';
      if (!list.length) {
        var empty = document.createElement('p');
        empty.className = 'card-note';
        empty.textContent = '还没有注册任何功能。';
        host.appendChild(empty);
      }
      list.forEach(function (tool) {
        var card = buildCard(tool);
        cards[tool.id] = card;
        host.appendChild(card.el);
      });
      built = true;
    }

    list.forEach(function (tool) {
      var card = cards[tool.id];
      if (!card) return;

      var tone = tool.status === 'idle' ? 'idle'
        : (tool.status === 'running' ? 'running' : 'warn');
      var badge = ADB.badge[tool.status] || ADB.badge.idle;
      ADB.paintBadge(card.badge, tool.status);

      card.brief.textContent = tool.brief || '';
      card.brief.style.color = '';
      if (tone === 'idle' && tool.stats && tool.stats.consecutiveFailures > 0) {
        card.brief.textContent += '（上次有连续失败，点开看日志）';
      }

      running[tool.id] = tool.status !== 'idle';
      card.start.disabled = running[tool.id];
      card.stop.disabled = !running[tool.id];
    });

    ADB.setConn('open', '已连接');
  }

  function act(id, action, btn) {
    var original = btn.textContent;
    btn.disabled = true;
    btn.textContent = action === 'start' ? '启动中…' : '停止中…';
    ADB.postJSON('/api/' + id + '/' + action, {})
      .then(refresh)
      .catch(function () { /* 刷新时会恢复按钮状态 */ })
      .then(function () {
        btn.textContent = original;
        btn.disabled = false;
        refresh();
      });
  }

  function refresh() {
    return ADB.getJSON('/api/tools')
      .then(function (data) { render(data.tools || []); })
      .catch(function () {
        ADB.setConn('retry', '后端没响应');
        built = false;   // 下次成功时重建，避免留下半截卡片
      });
  }

  $('#stop-all').addEventListener('click', function () {
    var ids = Object.keys(running).filter(function (id) { return running[id]; });
    if (!ids.length) return;
    Promise.all(ids.map(function (id) {
      return ADB.postJSON('/api/' + id + '/stop', {});
    })).then(refresh);
  });

  ADB.initTheme();
  refresh();
  setInterval(refresh, 4000);
})();
