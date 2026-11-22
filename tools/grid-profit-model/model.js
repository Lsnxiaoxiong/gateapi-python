/* Fixed-throughput return model; browser and Node, no external dependencies. */
(function (root) {
  'use strict';
  const defaults = Object.freeze({
    dailyTrades: 20, countMode: 'rounds', spacing: 1, takeProfit: 1.2,
    notional: 100, fee: 0.1, days: 30, initialPrice: 3000,
    dropPercent: 10, minQuantity: 0.001
  });
  const limits = {
    dailyTrades: [0, 1000000], spacing: [0.01, 30], takeProfit: [0.01, 100],
    notional: [0.01, 1e8], fee: [0, 5], days: [1, 3650],
    initialPrice: [0.000001, 1e9],
    dropPercent: [0, 99.999999], minQuantity: [0.00000001, 1e9]
  };
  function validate(input = {}) {
    const p = Object.fromEntries(Object.keys(defaults).map(key => [key, input[key] ?? defaults[key]]));
    for (const [key, [min, max]] of Object.entries(limits)) {
      if (!Number.isFinite(p[key]) || p[key] < min || p[key] > max) {
        throw new Error(`${key} 必须在 ${min} 至 ${max} 之间`);
      }
    }
    for (const key of ['days']) {
      if (!Number.isInteger(p[key])) throw new Error(`${key} 必须是整数`);
    }
    if (!['rounds', 'orders'].includes(p.countMode)) throw new Error('成交次数口径无效');
    return p;
  }
  function unitProfit(input) {
    const p = validate(input), b = p.takeProfit / 100, m = p.fee / 100;
    return p.notional * (b - m * (2 + b));
  }
  function gridCount(input) {
    const p = validate(input);
    const rawIndex = Math.log1p(-p.dropPercent / 100) / Math.log(1 - p.spacing / 100);
    const nearest = Math.round(rawIndex);
    const index = Math.abs(rawIndex - nearest) <= 64 * Number.EPSILON * Math.max(1, rawIndex)
      ? nearest : Math.floor(rawIndex);
    return index + 1; // Include j=0 at the starting price.
  }
  function project(input) {
    const p = validate(input), b = p.takeProfit / 100, m = p.fee / 100;
    const dailyRounds = p.dailyTrades / (p.countMode === 'orders' ? 2 : 1);
    const totalRounds = dailyRounds * p.days;
    const unitGross = p.notional * b, unitFees = p.notional * m * (2 + b);
    const unitNet = unitProfit(p), dailyNet = dailyRounds * unitNet;
    let cumulativeNotional = 0, cumulativeQuantity = 0, validGrids = 0;
    const count = gridCount(p);
    const levels = Array.from({ length: count }, (_, i) => {
      // Match the supplied Python loop: i=0 buys at the starting price.
      const buy = p.initialPrice * (1 - p.spacing / 100) ** i;
      const ratio = p.notional / (p.minQuantity * buy), nearest = Math.round(ratio);
      // Correct only floating-point noise at an exact minimum-quantity boundary.
      const steps = Math.abs(ratio - nearest) <= 4 * Number.EPSILON * Math.max(1, ratio)
        ? nearest : Math.floor(ratio);
      const quantity = steps * p.minQuantity, actualNotional = quantity * buy;
      cumulativeNotional += actualNotional; cumulativeQuantity += quantity;
      if (steps > 0) validGrids++;
      return { grid: i, buy, sell: buy * (1 + b), dropPercent: (1 - buy / p.initialPrice) * 100,
        quantity, actualNotional, cumulativeNotional, cumulativeQuantity, validGrids,
        cumulativeBudget: (i + 1) * p.notional };
    });
    const last = levels.at(-1);
    const scenario = {
      targetPrice: p.initialPrice * (1 - p.dropPercent / 100), touchedGrids: count,
      budgetNotional: count * p.notional,
      maxOpenGrids: last ? last.validGrids : 0,
      maxNotional: last ? last.cumulativeNotional : 0,
      totalQuantity: last ? last.cumulativeQuantity : 0,
      skippedGrids: count - (last ? last.validGrids : 0)
    };
    scenario.entryFees = scenario.maxNotional * m;
    scenario.requiredFunds = scenario.maxNotional * (1 + m);
    const points = Array.from({ length: p.days + 1 }, (_, day) => ({
      x: day, net: dailyNet * day, gross: dailyRounds * unitGross * day,
      fees: dailyRounds * unitFees * day, rounds: dailyRounds * day
    }));
    return { parameters: p, dailyRounds, totalRounds, totalOrders: totalRounds * 2,
      unitGross, unitFees, unitNet, dailyNet,
      dailyGross: dailyRounds * unitGross, dailyFees: dailyRounds * unitFees,
      totalNet: totalRounds * unitNet, totalGross: totalRounds * unitGross,
      totalFees: totalRounds * unitFees,
      buyCost: p.notional * (1 + m), gridBudget: cumulativeNotional * (1 + m),
      totalGridNotional: cumulativeNotional, totalGridQuantity: cumulativeQuantity,
      breakEvenPct: 2 * m / (1 - m) * 100,
      points, levels, scenario, gridCount: count };
  }
  const api = { defaults, limits, validate, unitProfit, gridCount, project };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.GridModel = api;
})(typeof self !== 'undefined' ? self : globalThis);
