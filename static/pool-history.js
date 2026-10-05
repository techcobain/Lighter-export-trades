// Pool deposits/withdrawals are transfer events, including L1 and forced exits.
const LighterPoolHistory = (() => {
    const actions = {
        L2MintShares: ['public', 'Deposit'],
        L2CreatePublicPool: ['public', 'Deposit'],
        L2BurnShares: ['public', 'Withdrawal'],
        L1BurnShares: ['public', 'Withdrawal'],
        L2ForceBurnShares: ['public', 'Forced Withdrawal'],
        L2StakeAsset: ['staking', 'Deposit'],
        L2CreateStakingPool: ['staking', 'Deposit'],
        L2UnstakeAsset: ['staking', 'Withdrawal'],
        L1UnstakeAsset: ['staking', 'Withdrawal'],
    };

    function classify(transfer) {
        const type = transfer.type.replace(/(?:Inflow|Outflow)$/, '');
        const info = actions[type];
        if (!info) return null;
        const [kind, action] = info;
        return {
            kind, action,
            pool_index: action === 'Deposit' ? transfer.to_account_index : transfer.from_account_index,
            direction: transfer.type.endsWith('Outflow') ? 'Outgoing' : 'Incoming',
        };
    }

    async function fetchHistory({ apiBase, authToken, accountIndex, fromTimestamp = null,
        toTimestamp = null, onProgress = () => {} }) {
        const transfers = [];
        const seenCursors = new Set();
        let cursor = null;
        let page = 0;
        while (true) {
            const params = new URLSearchParams({ account_index: accountIndex, type: 'all' });
            if (cursor) params.set('cursor', cursor);
            onProgress(`Account #${accountIndex}: Fetching transfer history page ${++page}...`);
            let response;
            for (let attempt = 0; attempt <= 5; attempt++) {
                response = await fetch(`${apiBase}/api/v1/transfer/history?${params}`, {
                    headers: { Authorization: authToken },
                });
                if (![429, 405].includes(response.status) || attempt === 5) break;
                const delay = Number(response.headers.get('Retry-After')) * 1000 || 15000;
                onProgress(`Account #${accountIndex}: Rate limited, waiting ${Math.ceil(delay / 1000)}s...`);
                await new Promise(resolve => setTimeout(resolve, delay));
            }
            if (!response.ok) throw new Error(`Transfer history failed (HTTP ${response.status}). Please try again.`);
            const data = await response.json();
            if (data.code !== 200 || !Array.isArray(data.transfers)) {
                throw new Error('Lighter could not return transfer history. Please try again.');
            }
            let reachedStart = false;
            for (const transfer of data.transfers) {
                if (fromTimestamp !== null && transfer.timestamp < fromTimestamp) reachedStart = true;
                else if (toTimestamp === null || transfer.timestamp <= toTimestamp) transfers.push(transfer);
            }
            if (!data.cursor || !data.transfers.length || reachedStart) break;
            if (seenCursors.has(data.cursor)) throw new Error('Transfer history pagination did not advance. Please try again.');
            seenCursors.add(data.cursor);
            cursor = data.cursor;
            await new Promise(resolve => setTimeout(resolve, 1000));
        }
        // The same transaction can have separate inflow/outflow records; retain both.
        const unique = new Map();
        for (const row of transfers) unique.set(`${row.id}:${row.type}:${row.asset_id}:${row.from_account_index}:${row.to_account_index}:${row.from_route}:${row.to_route}`, row);
        return Array.from(unique.values()).sort((a, b) => b.timestamp - a.timestamp);
    }

    return { classify, fetchHistory };
})();
