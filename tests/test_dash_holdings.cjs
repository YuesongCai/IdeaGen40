// Account scope and filtering invariants, executed against the shipped script.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const src=fs.readFileSync(require('node:path').join(__dirname,'../web/dash.html'),'utf8');
function fn(name){const start=src.indexOf(`function ${name}(`);assert.ok(start>=0);const end=src.indexOf('\nfunction ',start+1);return src.slice(start,end<0?undefined:end)}
function harness(){
  const s={HOLD:{query:'',ccy:'all',profit:'all',sort:'return_pct',ascending:false,subset:'all'},
    current:'holdings',PERF:{mode:'paper'},renderHoldings(){},pushDrawer(d){s.drawer=d}};
  vm.createContext(s);
  vm.runInContext(['holdFilteredPositions','holdSetSort','openPosDrawer','holdAmount'].map(fn).join('\n'),s);
  return s;
}
const book={equity:1000,cash:400,positions:[
  {code:'US.A',name:'Alpha',currency:'USD',unrealized:10,return_pct:2,weight_pct:20},
  {code:'HK.B',name:'Beta',currency:'HKD',unrealized:-20,return_pct:-5,weight_pct:25},
  {code:'US.C',name:'Gamma',currency:'USD',unrealized:30,return_pct:8,weight_pct:15},
  {code:'US.D',name:'Missing',currency:'USD',unrealized:null,return_pct:null,weight_pct:null}]};
test('return percent sorting puts missing values last in both directions',()=>{
  const s=harness();
  assert.deepEqual(Array.from(s.holdFilteredPositions(book,true),p=>p.code),['US.C','US.A','HK.B','US.D']);
  s.holdSetSort('return_pct:asc');
  assert.deepEqual(Array.from(s.holdFilteredPositions(book,true),p=>p.code),['HK.B','US.A','US.C','US.D']);
});
test('combined filters only hide rows; they do not renormalize weights or the account',()=>{
  const before=JSON.stringify(book),s=harness();
  s.HOLD.profit='gain';s.HOLD.ccy='USD';s.HOLD.query='gamma';
  const rows=s.holdFilteredPositions(book,true);
  assert.equal(rows.length,1);assert.equal(rows[0].weight_pct,15);
  assert.equal(JSON.stringify(book),before);
  assert.equal(s.holdFilteredPositions(book,false).length,4); // drawer remains complete
});
test('position navigation records both the account and accounting subset',()=>{
  const s=harness();s.HOLD.subset='live';s.openPosDrawer('US.A','alpha');
  assert.equal(s.drawer.p,'alpha|US.A|live');
  s.openPosDrawer('US.A','beta','backfill');assert.equal(s.drawer.p,'beta|US.A|backfill');
});
test('tiny negative percentages display as zero without hiding a negative cash amount',()=>{
  const s=harness();assert.equal(s.holdAmount(-0.0004),'0.00');assert.equal(s.holdAmount(-48.54),'-48.54');
});
