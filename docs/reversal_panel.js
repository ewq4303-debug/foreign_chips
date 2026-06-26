/* ===================================================================
 *  外資籌碼「轉折確認」訊號燈面板  reversal_panel.js
 *  讀 data/reversal_signals.json，渲染主燈 + 流量列 + 存量守門員 + 警告列。
 *  純 DOM + CSS，不需 ECharts。風格沿用現有暗色卡片。
 * =================================================================== */
(function () {
  "use strict";

  const C = { red: "#ff4d4f", yellow: "#faad14", green: "#52c41a", grey: "#8c8c8c" };
  const FLOW_LABEL = {
    S1_zscore_exit: "S1 Z 退出極端",
    S2_pc_reversal: "S2 P/C 反折",
    S3_premium_cross: "S3 賣權溢價收斂",
    S5_spot_turn: "S5 現貨轉買/急縮",
  };
  const WARN_LABEL = {
    W1_zscore_newhigh: "W1 Z 創新高",
    W2_real_distribution: "W2 真出貨",
    W3_futures_flip: "W3 期貨翻空",
  };
  const VERDICT_BLURB = {
    PANIC_CONTINUES: "警告訊號主導、結構未轉，恐慌延續中。",
    BOUNCE_BREWING: "流量情緒鬆動，但累積結構未翻，反彈僅供觀察。",
    REVERSAL_CONFIRMED: "流量與存量雙確認，轉折成立。",
    NEUTRAL: "訊號分歧或資料不足，先觀察。",
  };

  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function lamp(on, color, label, detail) {
    const wrap = el("div", "rv-lamp");
    wrap.title = detail || "";
    const dot = el("span", "rv-dot");
    dot.style.background = on ? color : C.grey;
    if (on) dot.style.boxShadow = "0 0 8px 1px " + color;
    wrap.appendChild(dot);
    wrap.appendChild(el("span", "rv-lab", label));
    return wrap;
  }

  function render(r) {
    const root = document.getElementById("reversal-panel");
    if (!root) return;
    root.innerHTML = "";

    const v = r.verdict || {};
    const color = C[v.color] || C.grey;
    const insufficient = r.insufficient_data;

    // 標題列
    const head = el("div", "rv-head");
    head.appendChild(el("span", "rv-tag", "轉折監控"));
    head.appendChild(el("span", "rv-title", "外資籌碼轉折確認訊號燈"));
    head.appendChild(el("span", "rv-hint",
      "流量 × 存量雙確認　資料日 " + (r.data_date || "—")));
    root.appendChild(head);

    // 主燈
    const main = el("div", "rv-main");
    const big = el("span", "rv-bigdot");
    big.style.background = insufficient ? C.grey : color;
    big.style.boxShadow = "0 0 16px 3px " + (insufficient ? C.grey : color);
    main.appendChild(big);
    const txt = el("div", "rv-maintext");
    const label = insufficient ? "資料不足" : (v.label || "—");
    txt.appendChild(el("b", null,
      "<span style='color:" + (insufficient ? C.grey : color) + "'>" + label + "</span>"));
    const blurb = insufficient
      ? "有效樣本不足，主燈轉灰。"
      : (VERDICT_BLURB[v.code] || "");
    txt.appendChild(el("span", "rv-blurb", blurb));
    main.appendChild(txt);
    if (!insufficient) {
      const score = el("div", "rv-score");
      score.appendChild(el("div", "rv-scoreval",
        (v.flow_score != null ? v.flow_score : 0) + "/" + (v.flow_max != null ? v.flow_max : 4)));
      score.appendChild(el("div", "rv-scorelab", "流量分數"));
      main.appendChild(score);
    }
    root.appendChild(main);

    // 流量列
    const flowRow = el("div", "rv-row");
    flowRow.appendChild(el("div", "rv-rowlab", "流量端 flow"));
    const flowLamps = el("div", "rv-lamps");
    const sig = r.signals || {};
    Object.keys(FLOW_LABEL).forEach(function (k) {
      const s = sig[k] || {};
      flowLamps.appendChild(lamp(s.triggered, C.green, FLOW_LABEL[k], s.detail));
    });
    flowRow.appendChild(flowLamps);
    flowRow.appendChild(el("div", "rv-rowtail",
      (v.flow_score != null ? v.flow_score : 0) + "/" + (v.flow_max != null ? v.flow_max : 4)));
    root.appendChild(flowRow);

    // 存量守門員列（強調框）
    const s4 = sig.S4_cum_turnup || {};
    const stockRow = el("div", "rv-row rv-stock");
    stockRow.appendChild(el("div", "rv-rowlab", "存量守門員 ⭐"));
    const stockLamps = el("div", "rv-lamps");
    stockLamps.appendChild(lamp(s4.triggered, C.green, "S4 累積 E_Total 翻揚", s4.detail));
    stockRow.appendChild(stockLamps);
    stockRow.appendChild(el("div", "rv-rowtail rv-gate",
      s4.triggered ? "結構已轉" : "結構未轉，反彈僅供觀察"));
    root.appendChild(stockRow);

    // 警告列
    const warnRow = el("div", "rv-row");
    warnRow.appendChild(el("div", "rv-rowlab", "持續警告 warning"));
    const warnLamps = el("div", "rv-lamps");
    const warn = r.warnings || {};
    Object.keys(WARN_LABEL).forEach(function (k) {
      const w = warn[k] || {};
      warnLamps.appendChild(lamp(w.triggered, C.red, WARN_LABEL[k], w.detail));
    });
    warnRow.appendChild(warnLamps);
    root.appendChild(warnRow);

    if (r.updated_at) {
      root.appendChild(el("div", "rv-foot",
        "門檻可調（環境變數）・更新 " + r.updated_at.slice(0, 16).replace("T", " ")));
    }
  }

  function boot() {
    fetch("data/reversal_signals.json", { cache: "no-store" })
      .then(function (x) { return x.json(); })
      .then(render)
      .catch(function (e) {
        const root = document.getElementById("reversal-panel");
        if (root) root.innerHTML =
          "<div class='rv-err'>轉折訊號載入失敗：" + e + "</div>";
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
