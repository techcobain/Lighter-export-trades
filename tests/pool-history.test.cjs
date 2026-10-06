const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.resolve(__dirname, '../static/pool-history.js'), 'utf8');

function poolContext(fetch) {
    const context = vm.createContext({ URLSearchParams, Date, fetch, setTimeout: callback => callback() });
    vm.runInContext(source, context);
    return { context, pool: vm.runInContext('LighterPoolHistory', context) };
}

const response = (data, status = 200) => ({ ok: status === 200, status, headers: { get: () => null }, json: async () => data });
const row = (id, type, timestamp) => ({ id, type, timestamp, asset_id: 3, amount: '10.00', fee: '0',
    from_account_index: type.includes('Mint') || type.includes('StakeAsset') ? 42 : 99,
    to_account_index: type.includes('Mint') || type.includes('StakeAsset') ? 99 : 42 });

test('classifies both cash flows for public pool and staking lifecycle events', () => {
    const { pool } = poolContext();
    for (const [type, kind, action] of [
        ['L2MintShares', 'public', 'Deposit'], ['L2CreatePublicPool', 'public', 'Deposit'],
        ['L2BurnShares', 'public', 'Withdrawal'], ['L1BurnShares', 'public', 'Withdrawal'],
        ['L2ForceBurnShares', 'public', 'Forced Withdrawal'], ['L2StakeAsset', 'staking', 'Deposit'],
        ['L2CreateStakingPool', 'staking', 'Deposit'], ['L2UnstakeAsset', 'staking', 'Withdrawal'],
        ['L1UnstakeAsset', 'staking', 'Withdrawal'],
    ]) {
        for (const flow of ['Inflow', 'Outflow']) {
            const input = { type: type + flow, from_account_index: 10, to_account_index: 20 };
            const result = pool.classify(input);
            assert.equal(result.kind, kind);
            assert.equal(result.action, action);
            assert.equal(result.direction, flow === 'Inflow' ? 'Incoming' : 'Outgoing');
            assert.equal(result.pool_index, action === 'Deposit' ? 20 : 10);
        }
    }
    for (const type of ['L2TransferInflow', 'L2TransferOutflow', 'L2SelfTransfer']) assert.equal(pool.classify({ type }), null);
});

test('paginates, filters UTC timestamps, and preserves different legs of the same transaction', async () => {
    const pages = [
        [row('a', 'L2MintSharesOutflow', 3000), row('b', 'L2UnstakeAssetInflow', 2500)],
        [row('b', 'L2UnstakeAssetInflow', 2500), row('b', 'L2UnstakeAssetOutflow', 2500), row('c', 'L1BurnSharesInflow', 1000)],
    ];
    const calls = [];
    const { pool } = poolContext((url, options) => {
        calls.push({ url, options });
        const data = pages.shift();
        return response({ code: 200, transfers: data, cursor: pages.length ? 'next' : 'later' });
    });
    const result = await pool.fetchHistory({ apiBase: 'https://mainnet.zklighter.elliot.ai', authToken: 'ro:synthetic', accountIndex: 42,
        fromTimestamp: 2000, toTimestamp: 2800 });
    assert.equal(result.length, 2);
    assert.equal(calls.length, 2);
    assert.equal(new URL(calls[1].url).searchParams.get('cursor'), 'next');
    assert.equal(new URL(calls[0].url).searchParams.get('type'), 'all');
    assert.equal(calls[0].options.headers.Authorization, 'ro:synthetic');
    assert.ok(!calls[0].url.includes('ro:'));
});

test('fails on API errors and stuck cursors without accepting partial results', async () => {
    for (const answer of [response({}, 500), response({ code: 400, transfers: [] })]) {
        const { pool } = poolContext(() => answer);
        await assert.rejects(pool.fetchHistory({ apiBase: 'https://example.test', authToken: 'ro:test', accountIndex: 42 }));
    }
    const { pool } = poolContext(() => response({ code: 200, transfers: [row('a', 'L2MintSharesOutflow', 1000)], cursor: 'stuck' }));
    await assert.rejects(pool.fetchHistory({ apiBase: 'https://example.test', accountIndex: 42 }), /pagination did not advance/);
});

test('retries rate limits with a bounded retry budget', async () => {
    let count = 0;
    const { pool } = poolContext(() => ++count === 1 ? response({}, 429) : response({ code: 200, transfers: [], cursor: '' }));
    const result = await pool.fetchHistory({ apiBase: 'https://example.test', accountIndex: 42 });
    assert.equal(result.length, 0);
    assert.equal(count, 2);
    const limited = poolContext(() => response({}, 405)).pool;
    await assert.rejects(limited.fetchHistory({ apiBase: 'https://example.test', accountIndex: 42 }), /HTTP 405/);
});

function appContext() {
    const elements = new Map();
    const element = key => {
        if (!elements.has(key)) {
            const classes = new Set();
            elements.set(key, { value: '', checked: false, disabled: false, innerHTML: '', textContent: '',
                style: {}, children: [], appendChild(child) { this.children.push(child); }, click() {}, remove() {},
                classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
                setAttribute(name, value) { this[name] = value; } });
        }
        return elements.get(key);
    };
    class FixedDate extends Date { constructor(...args) { super(...(args.length ? args : ['2026-10-05T00:30:00Z'])); } }
    const { context } = poolContext();
    context.Date = FixedDate;
    const blobs = [];
    context.Blob = Blob;
    context.URL = { createObjectURL: blob => { blobs.push(blob); return 'blob:test'; }, revokeObjectURL() {} };
    let created = 0;
    context.document = {
        body: element('body'),
        createElement: () => element(`created-${++created}`),
        getElementById: element,
        querySelector: element,
        querySelectorAll: selector => selector === '[data-preset]' || selector === '.timeframe-option'
            ? ['7D', '30D', '90D', '2026', '2025'].map(p => element(`[data-preset="${p}"]`)) : [],
    };
    const html = fs.readFileSync(path.resolve(__dirname, '../static/index.html'), 'utf8');
    const appSource = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
    vm.runInContext(appSource.slice(0, appSource.indexOf('// Initialize on page load')), context);
    return { context, element, blobs };
}

test('presets use UTC calendar dates and 2025 requests start at mainnet genesis', () => {
    const { context, element } = appContext();
    for (const [preset, start, end] of [
        ['7D', '2026-09-29', '2026-10-05'], ['30D', '2026-09-06', '2026-10-05'],
        ['90D', '2026-07-08', '2026-10-05'], ['2026', '2026-01-01', '2026-10-05'],
        ['2025', '2025-01-01', '2025-12-31'],
    ]) {
        vm.runInContext(`selectTimeframePreset('${preset}')`, context);
        assert.equal(element('dateFrom').value, start);
        assert.equal(element('dateTo').value, end);
        assert.equal(element('includeHours').checked, false);
        assert.equal(element(`[data-preset="${preset}"]`)['aria-pressed'], 'true');
        const range = vm.runInContext('getTimeframeParams()', context);
        assert.equal(range.fromTimestamp, Math.max(Date.parse(start + 'T00:00:00.000Z'), Date.UTC(2025, 0, 17)));
        assert.equal(range.toTimestamp, Date.parse(end + 'T23:59:59.999Z'));
    }
    vm.runInContext('clearTimeframePreset()', context);
    assert.equal(vm.runInContext('selectedPreset', context), null);
});

test('network switch clears credentials, account results, and asset caches', () => {
    const { context, element } = appContext();
    vm.runInContext(`readOnlyToken = 'ro:synthetic'; accountIndexes = [42]; assetCache = {3: {symbol: 'LIT'}};
        fetchedData = {42: {trades: []}}; poolActivityData.public = {42: {events: []}}; setNetwork('rh');`, context);
    assert.equal(vm.runInContext('LIGHTER_API', context), 'https://api.rh.lighter.xyz');
    assert.equal(vm.runInContext('readOnlyToken', context), null);
    assert.equal(vm.runInContext('accountIndexes.length', context), 0);
    assert.equal(vm.runInContext('Object.keys(assetCache).length', context), 0);
    assert.equal(vm.runInContext('Object.keys(fetchedData).length', context), 0);
    assert.equal(element('poolsCard').classList.contains('hidden'), true);
    vm.runInContext('setAllFetchButtonsDisabled(true)', context);
    assert.equal(element('networkToggle').disabled, true);
});

test('staking and pool events appear in transfer results, including previously unrecognized types', () => {
    const { context } = appContext();
    const result = vm.runInContext(`getFilteredTransfers(processTransfers([
        {id:'1',type:'L2StakeAssetOutflow',timestamp:1791158400000,asset_id:3,amount:'10',fee:'0'},
        {id:'2',type:'L1BurnSharesInflow',timestamp:1791158400000,asset_id:3,amount:'20',fee:'0'},
        {id:'3',type:'UnknownFutureType',timestamp:1791158400000,asset_id:3,amount:'30',fee:'0'}
    ]))`, context);
    assert.equal(result.length, 3);
    assert.match(result[0].type, /Staking Deposit/);
    assert.equal(result[0].datetime_utc, '2026-10-05 00:00:00 UTC');
    const markup = vm.runInContext(`buildPoolTable([{id:'<script>',action:'Deposit',direction:'Outgoing',pool_index:99,asset:'LIT',amount:'10',fee:'0'}])`, context);
    assert.ok(markup.includes('&lt;script&gt;'));
    assert.ok(!markup.includes('<script>'));
});

test('pool and staking fetches render separately and download CSV plus raw JSON', async () => {
    const { context, element, blobs } = appContext();
    const history = [row('mint', 'L2MintSharesOutflow', 1791158400000), row('stake', 'L2StakeAssetOutflow', 1791158400000),
        row('unstake', 'L1UnstakeAssetInflow', 1791158400000), row('transfer', 'L2TransferInflow', 1791158400000)];
    context.fetch = async url => response(url.includes('assetDetails')
        ? { code: 200, asset_details: [{ asset_id: 3, symbol: 'LIT' }] }
        : { code: 200, transfers: history, cursor: '' });
    vm.runInContext("accountIndexes = [42]; readOnlyToken = 'ro:synthetic';", context);
    element('check-42').checked = true;
    await vm.runInContext("fetchPoolActivity('public')", context);
    await vm.runInContext("fetchPoolActivity('staking')", context);
    assert.equal(vm.runInContext('poolActivityData.public[42].events.length', context), 1);
    assert.equal(vm.runInContext('poolActivityData.staking[42].events.length', context), 2);
    assert.equal(element('poolsCard').classList.contains('hidden'), false);
    assert.equal(element('stakingCard').classList.contains('hidden'), false);
    vm.runInContext("exportPoolActivity('public', 42, 'csv'); exportPoolActivity('staking', 42, 'json');", context);
    const csv = await blobs[0].text();
    assert.ok(csv.includes('"Pool Account"'));
    assert.ok(csv.includes('"mint","99","Deposit","Outgoing","LIT","10.00"'));
    const json = JSON.parse(await blobs[1].text());
    assert.equal(json.network, 'core');
    assert.equal(json.account_index, 42);
    assert.equal(json.total_events, 2);
    assert.equal(json.events[1].type, 'L1UnstakeAssetInflow');
});
