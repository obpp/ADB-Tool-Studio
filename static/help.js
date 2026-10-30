/* 使用说明 二级页面
 *
 * 功能卡片和顺序都来自 /api/tools，加了新工具这里自动出现一张卡；
 * 详细速查按工具 id 写在下面这张表里。表里没有条目的工具只会少一段
 * 「什么时候用 / 怎么用 / 踩坑 / 等价命令」，卡片本身不受影响。
 *
 * 写内容的原则：页面上的输入框已经有 label 和提示，这里不复述，
 * 只写页面上看不出来的东西 —— 为什么有这一步、什么时候不该用、踩过什么坑。
 */
(function () {
  'use strict';

  var ADB = window.ADB;
  var $ = ADB.$;

  var DETAILS = {
    always_adb: {
      when: '板子经常掉 root、产测环境要求一直保持 root。手工反复敲 adb root 太烦，' +
            '就让它在后台按间隔自己来。',
      steps: [
        '选目标设备 —— 多台时必须自己选，不选只有在「恰好一台在线」时才会自动选。',
        '在参数区调好间隔、force_adb 的值，点「保存参数」。',
        '点「开始」。它会一直循环，中途失败会按固定间隔重试，不用管它。'
      ],
      pitfalls: [
        '每一轮的顺序是：设置 force_adb → adb root → 等设备在 adbd 重启后回来 → adb shell sync。',
        'production build 上 adb root 可能返回 0，却打印 cannot run as root —— 这里只记一条警告，' +
          '不会假装成功。',
        '「停止」打断的是当前这一轮和轮间的等待，不会重启设备。'
      ],
      cmd: 'adb shell settings put global force_adb 1\n' +
           'adb root\n' +
           'adb wait-for-device\n' +
           'adb shell sync'
    },

    ssh_tunnel: {
      when: '设备接在你本机，但你人在远程服务器（或者 CI 机器）上，想直接在那边敲 adb。',
      steps: [
        '填远程主机和 SSH 用户名（页面顶部会实时显示实际要执行的 ssh 命令，可以直接抄走）。',
        '点「建立隧道」，看到日志里写隧道已建立。',
        '在远程主机上敲 adb devices，就能看到接在本机的设备。'
      ],
      pitfalls: [
        '本机必须有一个活着的 adb server 在监听 5037（adb start-server），隧道只是管道。',
        '远程端口被占用是常态：换一个「远程绑定端口」，并且在远程主机上 ' +
          'export ANDROID_ADB_SERVER_PORT=<新端口>，否则那边的 adb 客户端还是连 5037。',
        '勾「绑定到 0.0.0.0」需要远端 sshd 开 GatewayPorts，不然会绑定失败。',
        '「隧道已建立」是启发式判断：ssh -N 成功时什么都不输出，所以进程活过约 3 秒就算连上。'
      ],
      cmd: 'ssh -R 5037:127.0.0.1:5037 -N user@host'
    },

    script_runner: {
      when: '要在板子上跑一段检查/操作脚本，一次性的或者按间隔反复跑（老化、压测）。',
      steps: [
        '「导入脚本…」选文件，或者直接把文件拖到脚本库下方的任意位置。',
        '在脚本库点一下选中它，需要参数就填在「脚本参数」里（会跟着这个脚本保存）。',
        '点「执行选中脚本」；勾了「循环执行」就按间隔一轮一轮跑。'
      ],
      pitfalls: [
        '脚本是在设备上执行的，不是在本机跑的 —— 设备上得有自己的 sh 和要用的命令。',
        '每次执行都会重新推一份（文件名带脚本 id 前缀），不做「设备上已有就跳过」。',
        '「停止」只是断开 adb 连接，设备上正在跑的命令不一定跟着退出。',
        '参数里有空格的要加引号；引号没闭合会直接拒绝执行，不会推到板子上白跑一趟。',
        '导入时会把 CRLF 换成 LF —— 不换的话 Windows 上写的 .sh 到板子上必报 : not found。'
      ],
      cmd: 'adb push check.sh /data/local/tmp/ats-0d30815d-check.sh\n' +
           'adb shell sh /data/local/tmp/ats-0d30815d-check.sh'
    },

    replace_image: {
      when: '刷 boot / dtbo 这类分区来验证新内核。普通搬文件用「推送文件」，别用这个。',
      steps: [
        '建一个预设：一行写「镜像 → 目标分区」，一个镜像可以同时写 A/B 两个槽。',
        '选预设点「执行选中预设」，确认条会逐条列出「哪个镜像写到哪个分区」。',
        '点「确认烧写」才真的开始：先预检，再推送、核对字节数、dd，全部成功后才 reboot。'
      ],
      pitfalls: [
        '⚠ 写进分区就回不去了：中途失败或点停止，已经写过的分区不会回滚，A/B 两个槽可能对不上，' +
          '需要重新完整烧一次。',
        '⚠ 这个功能直接写块设备，写错了设备起不来。确认条上逐条核对分区名再点。',
        '会先把所有目标验一遍（test -b、blockdev 比大小），任何一道不过都不写分区，更不会重启。',
        '写 /dev/block/by-name/... 通常需要 adbd 已经是 root，没有权限时 dd 会失败并中止（不重启）。',
        '大镜像推得慢就把「单步超时」调大（默认 600 秒，上限 7200 秒）。'
      ],
      cmd: 'adb push boot.img /mnt/ats-47fb54bd-1-boot.img\n' +
           'adb shell "dd if=/mnt/ats-47fb54bd-1-boot.img ' +
           'of=/dev/block/by-name/boot_a bs=1048576"\n' +
           'adb shell reboot'
    },

    push_file: {
      when: '把本地文件搬到设备上的某个目录。不写分区、不重启、不保存预设。',
      steps: [
        '把文件拖进虚线框，或者点「选择文件…」多选。',
        '填目标目录（必须 / 开头），也可以点「浏览」在设备上选。',
        '点推送。一个文件失败不影响后面的，最后汇总成功几个、失败几个。'
      ],
      pitfalls: [
        '设备上的同名文件会被直接覆盖，日志里会写最终路径。',
        '文件是先上传到本机临时目录、再 adb push 过去的；本机的原文件不会被复制或改动。',
        '「停止」只打断正在推的那一个，已经推完的不回滚，当前这个可能只推了一半。',
        '单个文件上限 4 GB，推送超时按文件大小自动放宽。'
      ],
      cmd: 'adb push ./file.txt /data/local/tmp/'
    }
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* ---------------- 功能卡片 ---------------- */

  function buildCard(tool) {
    var card = el('a', 'feature-card');
    card.href = '/' + tool.page;
    card.title = tool.name + ' · ' + (tool.desc || '');

    var top = el('span', 'fc-top');
    top.appendChild(el('span', 'fc-icon', tool.icon || '•'));
    top.appendChild(el('span', 'fc-name', tool.name));
    card.appendChild(top);
    card.appendChild(el('span', 'fc-desc', tool.desc || ''));
    return card;
  }

  /* ---------------- 每个功能的速查 ---------------- */

  function row(key, body) {
    var wrap = el('div', 'doc-row');
    wrap.appendChild(el('span', 'doc-key', key));
    wrap.appendChild(body);
    return wrap;
  }

  function stepsList(items) {
    var box = el('ol', 'doc-body doc-list');
    items.forEach(function (text) { box.appendChild(el('li', '', text)); });
    return box;
  }

  function warnList(items) {
    var box = el('ul', 'doc-body doc-list doc-warn');
    items.forEach(function (text) { box.appendChild(el('li', '', text)); });
    return box;
  }

  function buildDoc(tool, detail) {
    var sec = el('section', 'tool-doc');
    sec.id = 'tool-' + tool.id;

    var head = el('div', 'tool-doc-head');
    head.appendChild(el('h3', '', tool.name));
    var open = el('a', 'btn ghost small doc-open', '打开页面 →');
    open.href = '/' + tool.page;
    head.appendChild(open);
    sec.appendChild(head);

    if (!detail) {
      sec.appendChild(el('p', 'help-note',
        '这个功能还没有写详细说明，先看页面上的输入框提示。'));
      return sec;
    }

    if (detail.when) sec.appendChild(row('什么时候用', el('p', 'doc-body', detail.when)));
    if (detail.steps) sec.appendChild(row('怎么用', stepsList(detail.steps)));
    if (detail.pitfalls) sec.appendChild(row('容易踩的坑', warnList(detail.pitfalls)));
    if (detail.cmd) sec.appendChild(row('等价命令', el('pre', 'doc-cmd', detail.cmd)));
    return sec;
  }

  /* ---------------- 启动 ---------------- */

  var grid = $('#feature-grid');
  var docs = $('#tool-docs');

  ADB.getJSON('/api/tools').then(function (data) {
    var tools = data.tools || [];
    if (!tools.length) {
      grid.appendChild(el('p', 'help-note', '后端没有注册任何功能。'));
      return;
    }
    // 顺序跟侧边栏、后端注册顺序一致，找起来才对得上号
    tools.forEach(function (tool) {
      grid.appendChild(buildCard(tool));
      docs.appendChild(buildDoc(tool, DETAILS[tool.id]));
    });
  }).catch(function () {
    grid.appendChild(el('p', 'help-note',
      '拿不到功能列表（后端没响应），下面的通用设置和常见问题仍然可以看。'));
  });
})();
