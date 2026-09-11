const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'../web/dash.html'),'utf8');
const source=html.slice(html.indexOf('function runningProgress(){'),html.indexOf('function chainChips(){'));
function progress(periods){
  const ctx={S:{periods,backtest:{summary:{live_vs_backfill:{n_live_periods:0,n_backfill_periods:6,periods_needed:4}}}}};
  vm.createContext(ctx);vm.runInContext(source,ctx);return ctx.runningProgress();
}
test('successful pending live run overrides stale research summary',()=>{
  const r=progress([{as_of:'2026-09-02',ok:1,classification:'backfill'},
    {as_of:'2026-09-09',ok:1,classification:'live',pending_orders:155,n_positions:0}]);
  assert.equal(r.n_live_periods,1);assert.equal(r.n_backfill_periods,1);
  assert.equal(r.periods_needed,3);assert.equal(r.byDate['2026-09-09'].pending_orders,155);
});
test('failed attempts and unknown classifications do not count as successful live runs',()=>{
  const r=progress([{as_of:'2026-09-09',ok:0,classification:'live'},
    {as_of:'2026-09-02',ok:1,classification:'unknown'}]);
  assert.equal(r.n_live_periods,0);assert.equal(r.periods_needed,4);
});
test('empty run history does not invent periods from research',()=>{
  const r=progress([]);assert.equal(r.n_backfill_periods,0);assert.equal(r.n_live_periods,0);
});
