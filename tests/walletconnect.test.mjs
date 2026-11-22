import assert from "node:assert/strict";
import test from "node:test";
import { permits, reviewedNamespaces } from "../src/xian_wallet_cli/walletconnect/policy.mjs";

const chain = "xian:local-test";
const account = "a".repeat(64);

test("only reviewed chains and methods are permitted", () => {
  const namespaces = reviewedNamespaces(
    { xian: { chains: [chain], methods: ["xian_sendCall"], events: ["disconnect"] } },
    chain, account,
  );
  assert.equal(permits({ namespaces }, chain, account, chain, "xian_sendCall"), true);
  assert.equal(permits({ namespaces }, chain, account, chain, "xian_signMessage"), false);
  assert.equal(permits({ namespaces }, chain, account, "xian:other", "xian_sendCall"), false);
  assert.equal(permits({ namespaces, expiry: 1 }, chain, account, chain, "xian_sendCall"), false);
  assert.equal(permits({ namespaces }, chain, "b".repeat(64), chain, "xian_sendCall"), false);
  assert.throws(() => reviewedNamespaces({ xian: { chains: [chain], methods: ["unknown"] } }, chain, account));
  assert.throws(() => reviewedNamespaces({ eip155: { chains: ["eip155:1"], methods: ["eth_sendTransaction"] } }, chain, account));
});
