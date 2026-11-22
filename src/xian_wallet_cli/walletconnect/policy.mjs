export const supported = new Set([
  "xian_requestAccounts", "xian_accounts", "xian_chainId", "xian_getWalletInfo",
  "xian_signMessage", "xian_prepareTransaction", "xian_signTransaction",
  "xian_sendTransaction", "xian_sendCall",
]);

const supportedEvents = new Set(["accountsChanged", "chainChanged", "connect", "disconnect"]);

export function reviewedNamespaces(required, chain, account) {
  const events = new Set();
  const methods = new Set();
  const requested = Object.entries(required ?? {});
  if (!requested.length) throw new Error("The dapp must request an explicit Xian scope.");
  for (const [key, scope] of requested) {
    const chains = key.includes(":") ? [key] : scope.chains ?? [];
    if (key.split(":")[0] !== "xian" || !chains.length || chains.some((id) => id !== chain)) {
      throw new Error("The dapp requested an unsupported network.");
    }
    for (const event of scope.events ?? []) {
      if (!supportedEvents.has(event)) throw new Error("The dapp requested an unsupported event.");
      events.add(event);
    }
    for (const method of scope.methods ?? []) {
      if (!supported.has(method)) throw new Error("The dapp requested an unsupported method.");
      methods.add(method);
    }
  }
  if (!methods.size) throw new Error("The dapp must request an explicit wallet method.");
  return { xian: { accounts: [`${chain}:${account}`], chains: [chain], methods: [...methods], events: [...events] } };
}

export function permits(session, chain, account, requestChain, method) {
  if (Number.isFinite(session?.expiry) && session.expiry <= Date.now() / 1000) return false;
  const scope = session?.namespaces?.xian;
  return requestChain === chain && supported.has(method)
    && Boolean(scope?.methods.includes(method) && scope.accounts.includes(`${chain}:${account}`));
}
