/* 版式走查：把这个文件整段贴进运行台的控制台，然后 `layoutAudit()`。
 *
 * 为什么要有它：CSS 栅格和 HTML 表格错位一格时，页面照常渲染、控制台不报错、
 * Python 测试全绿——只有人眼看得出来，而人眼要盯 5 个视图 × 14 个分区 ×
 * 16 类抽屉 × 若干宽度。2026-09-05 凌晨那次（持仓行五个格子塞进四列栅格）
 * 是隔了几小时才被看到的。
 *
 * tests/test_dash_layout.py 钉的是**源码里**的契约（列数 == 格子数），这个脚本
 * 查的是**渲染之后**的事实：真的换行了没有、真的出界了没有、键盘真的到得了没有。
 * 两者互补，都跑一遍才算走查完。
 *
 *   layoutAudit()            // 当前宽度，全部视图 + 抽屉
 *   layoutAudit([1440,1100]) // 指定几档宽度（需要能改窗口大小时才有意义）
 *
 * 已知的、**不是 bug** 的命中（看到它们不用管）：
 *   g.pc-click            流水线图的 SVG 节点没有 tabindex——它们打开的抽屉
 *                         在「主链路」那几颗真按钮上都有等价入口，给 132 个
 *                         节点各加一个 Tab 停顿是净损失
 *   span.race-track       名次圆点用 translate(-50%) 居中在它的分值上，两端
 *                         会探出轨道 2-4px。夹住反而会谎报位置
 *   div.pos-headr:4 / div.pos-line:5
 *                         ≤1180px 那一档刻度条整条挪到第二行，表头因此少一格
 *   div.pgrid > *         网格在抽屉里被压到 min-content 以下，靠 .pgrid-wrap
 *                         横向滚动露出来——滚到底能看到「合计」就是对的
 */
(function (global) {
  function sig(el) {
    var s = (el.tagName || '?').toLowerCase();
    var c = el.getAttribute && el.getAttribute('class');
    if (c && typeof c === 'string') s += '.' + c.trim().split(/\s+/).slice(0, 3).join('.');
    return s;
  }

  function auditOnce(tag) {
    var out = [], seen = {}, byTpl = {};
    function push(o) {
      var k = o.type + '|' + o.el + '|' + (o.tracks || '');
      if (seen[k]) return;
      seen[k] = 1; out.push(o);
    }
    var all = document.querySelectorAll('body *');
    for (var i = 0; i < all.length; i++) {
      var e = all[i], r = e.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      var cs = getComputedStyle(e);
      if (cs.visibility === 'hidden') continue;

      if (cs.display === 'grid' || cs.display === 'inline-grid') {
        var tracks = cs.gridTemplateColumns.split(/\s+/).filter(Boolean).length;
        var kids = [];
        for (var j = 0; j < e.children.length; j++) {
          var kr = e.children[j].getBoundingClientRect();
          if (kr.width > 0 && kr.height > 0) kids.push([e.children[j], kr]);
        }
        if (kids.length) {
          var explicit = false;
          for (var m = 0; m < kids.length; m++) {
            var ks = getComputedStyle(kids[m][0]);
            if (ks.gridColumn !== 'auto' || ks.gridRow !== 'auto') { explicit = true; break; }
          }
          /* 真的换行 = DOM 序里 left 回退了。不能数 top 的种数：align-items:center
             下高矮不同的格子 top 本来就不一样，那不是换行。 */
          var wraps = 0;
          for (var w = 1; w < kids.length; w++)
            if (kids[w][1].left < kids[w - 1][1].left - 0.5) wraps++;
          if (!explicit && tracks > 1 && kids.length > tracks && kids.length % tracks !== 0)
            push({ type: 'ragged-grid', tag: tag, el: sig(e), tracks: tracks, kids: kids.length,
                   txt: e.textContent.trim().slice(0, 44) });
          if (!explicit && tracks > 1 && kids.length <= tracks && wraps > 0)
            push({ type: 'unexpected-wrap', tag: tag, el: sig(e), tracks: tracks, kids: kids.length,
                   txt: e.textContent.trim().slice(0, 44) });
          for (var s2 = 0; s2 < kids.length; s2++) {
            var b = kids[s2][1];
            if (b.right > r.right + 1.5 || b.left < r.left - 1.5)
              push({ type: 'cell-spill', tag: tag, el: sig(e) + ' > ' + sig(kids[s2][0]),
                     over: Math.round(Math.max(b.right - r.right, r.left - b.left)),
                     txt: (kids[s2][0].textContent || '').trim().slice(0, 30) });
          }
          var key = (e.parentElement ? sig(e.parentElement) : '') + '|' + cs.gridTemplateColumns;
          (byTpl[key] = byTpl[key] || []).push(sig(e) + ':' + kids.length);
        }
      }

      /* 横向溢出。有意的省略号和 line-clamp 不算——那是设计，不是事故。 */
      if (e instanceof HTMLElement && cs.overflowX !== 'auto' && cs.overflowX !== 'scroll'
          && e.clientWidth > 0 && e.scrollWidth > e.clientWidth + 1) {
        var ellipsis = cs.textOverflow === 'ellipsis' && cs.overflowX === 'hidden';
        var clamped = cs.webkitLineClamp && cs.webkitLineClamp !== 'none';
        if (!ellipsis && !clamped)
          push({ type: cs.overflowX === 'hidden' ? 'clipped' : 'spill', tag: tag, el: sig(e),
                 over: e.scrollWidth - e.clientWidth, txt: e.textContent.trim().slice(0, 36) });
      }

      /* 被挤成零宽的文本 */
      if (!e.children.length && r.width < 6 && e instanceof HTMLElement) {
        var t = (e.textContent || '').trim();
        if (t.length > 1)
          push({ type: 'zero-width-text', tag: tag, el: sig(e), w: Math.round(r.width), txt: t.slice(0, 26) });
      }
    }

    /* 同一个父容器下、同一套列宽，格数却不一样——表头和数据行错位的经典形态 */
    for (var k2 in byTpl) {
      var v = byTpl[k2], uniq = v.filter(function (x, ix) { return v.indexOf(x) === ix });
      var counts = {}; v.forEach(function (x) { counts[x.split(':')[1]] = 1 });
      if (v.length > 1 && Object.keys(counts).length > 1)
        push({ type: 'sibling-grid-count-differs', tag: tag, el: uniq[0], variants: uniq.slice(0, 5) });
    }

    /* 需要横向滚动才看得到的列 */
    for (var s3 = 0, sc = document.querySelectorAll('*'); s3 < sc.length; s3++) {
      var sw = sc[s3], scs = getComputedStyle(sw);
      if (scs.overflowX !== 'auto' && scs.overflowX !== 'scroll') continue;
      if (sw.clientWidth <= 0) continue;
      var hid = sw.scrollWidth - sw.clientWidth;
      if (hid > 8) {
        var wr = sw.getBoundingClientRect();
        var cut = [].slice.call(sw.querySelectorAll('thead th'))
          .filter(function (th) { return th.getBoundingClientRect().right > wr.right + 1 });
        push({ type: 'needs-hscroll', tag: tag, el: sig(sw), hidden: hid, cw: sw.clientWidth,
               colsOffscreen: cut.length,
               firstCut: cut[0] ? (cut[0].textContent || '').trim().slice(0, 12) : null });
      }
    }

    /* 键盘到不了的可点元素。遮罩不算——Esc 就是它的键盘等价。 */
    for (var c2 = 0, cl = document.querySelectorAll('[onclick]'); c2 < cl.length; c2++) {
      var ce = cl[c2], cr = ce.getBoundingClientRect();
      if (cr.width <= 0 || cr.height <= 0) continue;
      var tn = ce.tagName.toLowerCase();
      if (tn === 'button' || (tn === 'a' && ce.hasAttribute('href'))) continue;
      if (ce.classList.contains('drawer-scrim')) continue;
      if (ce.hasAttribute('tabindex') && ce.getAttribute('tabindex') !== '-1') continue;
      push({ type: 'click-not-keyboard', tag: tag, el: sig(ce),
             txt: (ce.textContent || '').trim().slice(0, 26) });
    }
    return out;
  }

  /* 每个视图的每个分区、每一类抽屉都要真的渲染出来才查得到。
     不用 setTimeout：渲染是同步的，读一次 getBoundingClientRect 就够了；
     而标签页不在前台时 rAF/setTimeout 会被节流到几十秒，等不到。 */
  function sweep(label) {
    var found = [];
    function run(tag) {
      document.body.getBoundingClientRect();
      if (typeof armRowKeys === 'function') armRowKeys();
      found.push.apply(found, auditOnce(label + ' ' + tag));
    }
    var plan = [
      ['overview', ['verdict', 'status', 'runs']],
      ['holdings', ['exposure']],
      ['method', ['mainline', 'canvas', 'corpus', 'universe']],
      ['evidence', ['strategies', 'backtest', 'limits', 'receipt']],
      ['rules', ['teach', 'running']]
    ];
    plan.forEach(function (p) {
      p[1].forEach(function (sec) {
        try { gotoSection(p[0], sec); closeDrawers(); } catch (e) {}
        run(p[0] + '/' + sec);
      });
    });
    function drawer(tag, open) {
      try { closeDrawers(); open(); run(tag); }
      catch (e) { found.push({ type: 'ERROR', tag: label + ' ' + tag, el: String(e).slice(0, 70) }); }
    }
    for (var i = 0; i < 6; i++) (function (n) { drawer('stage' + n, function () { openStageDrawer(n) }) })(i);
    var books = currentBooks();
    var codes = [];
    books.forEach(function (b) {
      (b.open_positions || []).forEach(function (p) { if (codes.indexOf(p.code) < 0) codes.push(p.code) });
    });
    (topicChosen('hgep') || []).slice(0, 4).forEach(function (t) {
      drawer('topic:' + t, function () { openTopicDrawer(t) });
    });
    books.slice(0, 4).forEach(function (b) {
      drawer('book:' + b.selector, function () { openBookDrawer(b.selector) });
    });
    codes.slice(0, 4).forEach(function (c) { drawer('pos:' + c, function () { openPosDrawer(c) }) });
    codes.slice(0, 2).forEach(function (c) { drawer('props:' + c, function () { openProposalsDrawer(c) }) });
    ['cal', 'trust', 'status', 'asklog', 'askpick'].forEach(function (k) {
      drawer('d:' + k, function () { drawerStack = [{ t: k, p: '' }]; renderDrawers() });
    });
    try { closeDrawers() } catch (e) {}
    return found;
  }

  global.layoutAudit = function (widths) {
    var all = [];
    if (widths && widths.length) {
      console.warn('浏览器里改不了窗口宽度：请手动调到 ' + widths.join(' / ') + '，每档跑一次 layoutAudit()');
    }
    all = sweep(innerWidth);
    var uniq = {}, byType = {};
    all.forEach(function (x) {
      uniq[x.type + '|' + x.el] = x;
      byType[x.type] = (byType[x.type] || 0) + 1;
    });
    var rows = Object.keys(uniq).map(function (k) { return uniq[k] });
    console.log('宽度 ' + innerWidth + '：命中 ' + rows.length + ' 类', byType);
    if (rows.length) console.table(rows);
    else console.log('干净。');
    return rows;
  };
})(window);
