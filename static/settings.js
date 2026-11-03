/* ADB Tool Studio —— 设置页：数据目录
 *
 * 两条路：
 *   复制并切换 —— 先把「要复制多少文件、复制到哪」说清楚，再确认。
 *                 目标里已有同名文件时后端回 409 + conflicts，确认条
 *                 换成覆盖版本，用户再点一次才带 overwrite=true 重发。
 *   使用已有目录 —— 先 dryRun 让后端只检查目标目录，列出它已有的东西，
 *                 确认后只写指针，不复制、不修改那个目录。
 *
 * 切换完由后端写指针、重建工具实例，本页跟着刷新读数；
 * 其他已经打开的页面不会自动换目录，所以成功后提示刷新。
 */
(function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var info = null;
  var pending = null;        // { path, reset, overwrite } / { mode: 'adopt', path, preview }
  var busy = false;
  var firstLoad = true;

  var DIR_LABEL = {
    config: '配置与预设',
    scripts: '脚本',
    files: '推送文件副本',
    logs: '日志'
  };

  /* ---------------- 小工具 ---------------- */

  function code(text) {
    var el = document.createElement('code');
    el.textContent = text;
    return el;
  }

  function sizeText(bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    var units = ['KB', 'MB', 'GB', 'TB'];
    var i = -1;
    do {
      n /= 1024;
      i++;
    } while (n >= 1024 && i < units.length - 1);
    return (n >= 10 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }

  function setStatus(text, isError) {
    var el = $('#dir-status');
    el.textContent = text || '';
    if (isError) el.setAttribute('data-tone', 'error');
    else el.removeAttribute('data-tone');
  }

  function running() {
    return info && info.running ? info.running : [];
  }

  function syncButtons() {
    var blocked = busy || running().length > 0;
    var confirming = !$('#confirm').hidden;
    $('#btn-migrate').disabled = blocked || confirming;
    $('#btn-adopt').disabled = blocked || confirming;
    $('#btn-browse').disabled = busy || confirming;
    // 确认条开着时别让人改路径，不然确认的和输入框里的对不上
    $('#new-dir').disabled = busy || confirming;
    $('#btn-confirm-migrate').disabled = busy;
    $('#btn-confirm-cancel').disabled = busy;

    // 已经在默认目录时没什么可恢复的；但指针坏了（error）要能清掉
    var canReset = !!(info && (info.isCustom || info.error));
    $('#btn-reset').hidden = !canReset;
    $('#btn-reset').disabled = blocked;
  }

  /* ---------------- 渲染 ---------------- */

  function renderPreview(data) {
    $('#migrate-preview').hidden = false;
    var body = $('#preview-body');
    body.textContent = '';
    var dirs = data.dirs || {};

    Object.keys(DIR_LABEL).forEach(function (name) {
      var stat = dirs[name] || { files: 0, bytes: 0 };
      var tr = document.createElement('tr');
      var label = document.createElement('td');
      label.textContent = DIR_LABEL[name];
      var files = document.createElement('td');
      files.className = 'num';
      files.textContent = ADB.num(stat.files);
      var size = document.createElement('td');
      size.className = 'num';
      size.textContent = sizeText(stat.bytes);
      tr.appendChild(label);
      tr.appendChild(files);
      tr.appendChild(size);
      body.appendChild(tr);
    });

    var total = document.createElement('tr');
    var totalLabel = document.createElement('td');
    totalLabel.textContent = '合计';
    var totalFiles = document.createElement('td');
    totalFiles.className = 'num';
    totalFiles.textContent = ADB.num(data.totalFiles);
    var totalSize = document.createElement('td');
    totalSize.className = 'num';
    totalSize.textContent = sizeText(data.totalBytes);
    total.appendChild(totalLabel);
    total.appendChild(totalFiles);
    total.appendChild(totalSize);
    body.appendChild(total);
  }

  function render(data) {
    info = data;

    $('#cur-dir').textContent = data.dataDir || '—';
    $('#default-dir').textContent = data.defaultDir || '—';
    $('#logs-dir').textContent = data.logsDir || '—';

    if (data.error) {
      ADB.paintBadge($('#where-badge'), 'idle', {
        tone: 'warn', icon: '⚠', text: '已回退到默认位置'
      });
    } else if (data.isCustom) {
      ADB.paintBadge($('#where-badge'), 'idle', {
        tone: 'running', icon: '●', text: '自定义位置'
      });
    } else {
      ADB.paintBadge($('#where-badge'), 'idle', {
        tone: 'idle', icon: '■', text: '默认位置'
      });
    }

    var note = $('#dir-note');
    if (data.error) {
      note.hidden = false;
      note.setAttribute('data-tone', 'error');
      note.textContent = data.error;
    } else {
      note.hidden = true;
      note.removeAttribute('data-tone');
      note.textContent = '';
    }

    if (firstLoad) {
      firstLoad = false;
      var input = $('#new-dir');
      if (!input.value.trim()) input.value = data.dataDir || '';
    }

    renderPreview(data);

    if (running().length) {
      if (!$('#confirm').hidden) hideConfirm();
      setStatus(running().join('、') + ' 正在运行，先全部停止再切换', true);
    }
    syncButtons();
  }

  function load() {
    return ADB.getJSON('/api/settings').then(function (data) {
      if (!data || !data.ok) throw new Error('设置读取失败');
      render(data);
      return data;
    }).catch(function () {
      ADB.paintBadge($('#where-badge'), 'idle', {
        tone: 'error', icon: '⚠', text: '读取失败'
      });
      setStatus('读不到设置，后端没响应。刷新页面试试。', true);
    });
  }

  /* ---------------- 确认条 ---------------- */

  function hideConfirm() {
    pending = null;
    $('#confirm').hidden = true;
    syncButtons();
  }

  function showConfirm(opts) {
    pending = opts;

    var text = $('#confirm-text');
    text.textContent = '';
    var note = $('#confirm-note');
    note.textContent = '';

    var target = opts.reset
      ? (info ? info.defaultDir : '默认位置')
      : opts.path;

    if (opts.overwrite) {
      var count = opts.conflictCount || opts.conflicts.length;
      text.appendChild(document.createTextNode(
        '目标里已有 ' + count + ' 个同名文件，继续复制会覆盖它们：'));
      var list = document.createElement('ul');
      list.className = 'confirm-list';
      opts.conflicts.forEach(function (rel) {
        var li = document.createElement('li');
        li.appendChild(code(rel));
        list.appendChild(li);
      });
      text.appendChild(list);
      note.textContent = (opts.conflicts.length < count
        ? '只列出前 ' + opts.conflicts.length + ' 个。' : '') +
        '只覆盖新位置里的同名文件，旧目录里的原件还在。';
      $('#btn-confirm-migrate').textContent = '覆盖并复制';
    } else if (opts.mode === 'adopt') {
      var preview = opts.preview || {};
      var dirs = preview.dirs || {};
      text.appendChild(document.createTextNode(
        '直接使用这个目录作为数据目录：'));
      text.appendChild(code(target));
      var found = preview.found || [];
      if (found.length) {
        var adoptList = document.createElement('ul');
        adoptList.className = 'confirm-list';
        found.forEach(function (name) {
          var stat = dirs[name] || { files: 0 };
          var li = document.createElement('li');
          li.appendChild(document.createTextNode(DIR_LABEL[name] + ' '));
          li.appendChild(code(name + '/'));
          li.appendChild(document.createTextNode(
            '：' + ADB.num(stat.files) + ' 个文件'));
          adoptList.appendChild(li);
        });
        text.appendChild(adoptList);
      }
      if (preview.totalFiles !== undefined) {
        text.appendChild(document.createTextNode(
          '共 ' + ADB.num(preview.totalFiles) + ' 个文件（' +
          sizeText(preview.totalBytes) + '）。'));
      }
      note.textContent = '当前数据目录不会被修改，也不会复制任何文件。' +
        '切换后立即生效，其他已打开的页面需要刷新。';
      $('#btn-confirm-migrate').textContent = '直接使用';
    } else if (opts.reset && info && !info.isCustom) {
      text.appendChild(document.createTextNode(
        '当前实际使用的就是默认位置，这一步只清除 data_dir.json 里的失效记录，不复制文件。'));
      note.textContent = '清除后启动时不再提示回退。';
      $('#btn-confirm-migrate').textContent = '确认清除';
    } else if (opts.reset) {
      text.appendChild(document.createTextNode(
        '把 ' + ADB.num(info ? info.totalFiles : 0) + ' 个文件（' +
        sizeText(info ? info.totalBytes : 0) + '）复制回默认位置 '));
      text.appendChild(code(target));
      text.appendChild(document.createTextNode('。'));
      note.textContent = '旧目录不会删除。复制完成后立即切换，其他已打开的页面需要刷新。';
      $('#btn-confirm-migrate').textContent = '确认复制';
    } else {
      text.appendChild(document.createTextNode(
        '把 ' + ADB.num(info ? info.totalFiles : 0) + ' 个文件（' +
        sizeText(info ? info.totalBytes : 0) + '）复制到 '));
      text.appendChild(code(target));
      text.appendChild(document.createTextNode('。'));
      note.textContent = '旧目录不会删除。复制完成后立即生效，其他已打开的页面需要刷新。';
      $('#btn-confirm-migrate').textContent = '确认复制';
    }

    $('#confirm').hidden = false;
    syncButtons();
    $('#btn-confirm-migrate').focus();
  }

  /* ---------------- 动作 ---------------- */

  function afterSwitch(data) {
    var input = $('#new-dir');
    input.value = data.dataDir || '';
    if (data.adopted) {
      setStatus(
        '已切换到 ' + (data.dataDir || '所选目录') +
        '，直接使用该目录里已有的配置（未复制文件），立即生效。' +
        '其他已打开的页面刷新一下。');
    } else {
      setStatus(
        '已切换到 ' + (data.dataDir || '新位置') +
        '，复制了 ' + ADB.num(data.copied ? data.copied.files : 0) +
        ' 个文件，立即生效。其他已打开的页面刷新一下。');
    }
    load().then(syncButtons);
  }

  function requestSwitch(opts) {
    var body;
    if (opts.mode === 'adopt') {
      body = { mode: 'adopt', path: opts.path };
    } else if (opts.reset) {
      body = { reset: true, overwrite: opts.overwrite };
    } else {
      body = { path: opts.path, overwrite: opts.overwrite };
    }

    busy = true;
    syncButtons();
    setStatus(opts.mode === 'adopt'
      ? '正在切换…'
      : (opts.overwrite ? '正在覆盖复制…' : '正在复制，文件多时要等一会儿…'));

    ADB.postJSON('/api/settings/data-dir', body).then(function (data) {
      busy = false;

      if (data && data.ok) {
        hideConfirm();
        afterSwitch(data);
        return;
      }

      if (data && data.conflicts && data.conflicts.length) {
        // 冲突确认条不关：换成覆盖版本，再点一次才真的覆盖
        showConfirm({
          path: opts.path,
          reset: opts.reset,
          overwrite: true,
          conflicts: data.conflicts,
          conflictCount: data.conflictCount
        });
        setStatus(data.error || '目标里已有同名文件', true);
        syncButtons();
        return;
      }

      hideConfirm();
      setStatus((data && data.error) || '切换失败', true);
      syncButtons();
      // 可能是别的页面刚启动了工具，重新读一次状态好让按钮跟着禁用
      load();
    }).catch(function () {
      busy = false;
      hideConfirm();
      setStatus('请求失败，后端还在吗？', true);
      syncButtons();
    });
  }

  function requestAdoptPreview(path) {
    busy = true;
    syncButtons();
    setStatus('正在检查目录…');

    ADB.postJSON('/api/settings/data-dir', {
      mode: 'adopt', path: path, dryRun: true
    }).then(function (data) {
      busy = false;
      if (data && data.ok) {
        setStatus('');
        showConfirm({ mode: 'adopt', path: path, preview: data });
        return;
      }
      hideConfirm();
      setStatus((data && data.error) || '这个目录不能用', true);
      syncButtons();
      load();
    }).catch(function () {
      busy = false;
      hideConfirm();
      setStatus('请求失败，后端还在吗？', true);
      syncButtons();
    });
  }

  /* ---------------- 事件 ---------------- */

  $('#dir-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (busy) return;
    if (running().length) {
      setStatus(running().join('、') + ' 正在运行，先全部停止再切换', true);
      return;
    }
    var path = $('#new-dir').value.trim();
    if (!path) {
      setStatus('先填一个目录', true);
      $('#new-dir').focus();
      return;
    }
    setStatus('');
    showConfirm({ path: path, reset: false, overwrite: false });
  });

  $('#btn-adopt').addEventListener('click', function () {
    if (busy) return;
    if (running().length) {
      setStatus(running().join('、') + ' 正在运行，先全部停止再切换', true);
      return;
    }
    var path = $('#new-dir').value.trim();
    if (!path) {
      setStatus('先填一个目录', true);
      $('#new-dir').focus();
      return;
    }
    setStatus('');
    requestAdoptPreview(path);
  });

  $('#btn-reset').addEventListener('click', function () {
    if (busy) return;
    if (running().length) {
      setStatus(running().join('、') + ' 正在运行，先全部停止再切换', true);
      return;
    }
    setStatus('');
    showConfirm({ reset: true, overwrite: false });
  });

  $('#btn-browse').addEventListener('click', function () {
    if (busy) return;
    var api = window.pywebview && window.pywebview.api;
    if (!api || typeof api.pick_folder !== 'function') {
      setStatus('网页版拿不到完整路径，请手动填写或粘贴目录', true);
      $('#new-dir').focus();
      return;
    }
    api.pick_folder($('#new-dir').value.trim()).then(function (path) {
      if (path) $('#new-dir').value = path;
    }).catch(function () {
      setStatus('打开文件夹对话框失败，可以直接填路径', true);
    });
  });

  $('#btn-confirm-cancel').addEventListener('click', function () {
    hideConfirm();
    $('#new-dir').focus();
  });

  $('#btn-confirm-migrate').addEventListener('click', function () {
    if (busy || !pending) return;
    requestSwitch(pending);
  });

  load();
})();
