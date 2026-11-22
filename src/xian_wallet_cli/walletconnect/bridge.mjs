import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline/promises";
import process from "node:process";
import { WalletKit } from "@reown/walletkit";
import { Core } from "@walletconnect/core";

import { permits, reviewedNamespaces } from "./policy.mjs";

const [wallet, home, network, chainId, accountName, address, uriFile] = process.argv.slice(2);
const chain = `xian:${chainId}`;
const baseArgs = ["--home", home, "--network", network];
const uri = (await readFile(uriFile, "utf8")).trim();
const core = new Core({ projectId: process.env.XIAN_WALLETCONNECT_PROJECT_ID, logger: "silent" });
const kit = await WalletKit.init({
  core,
  metadata: {
    name: "Xian Wallet CLI",
    description: "Xian terminal wallet",
    url: "https://xian.org",
    icons: [],
  },
});

let work = Promise.resolve();
function serial(handler) {
  work = work.then(handler).catch(() => {
    process.stderr.write("WalletConnect request could not be completed.\n");
  });
}

function cli(args, interactive = false) {
  return new Promise((resolve, reject) => {
    const options = { stdio: interactive ? "inherit" : ["ignore", "pipe", "ignore"] };
    const child = spawn(wallet, [...(interactive ? [] : ["--json"]), ...baseArgs, ...args], options);
    let stdout = "";
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk) => {
      stdout += chunk;
      if (stdout.length > 1000000) child.kill();
    });
    child.once("error", reject);
    child.once("close", (code) => {
      if (interactive) {
        if (code === 0) resolve(null);
        else reject(new Error("The wallet did not approve the request."));
        return;
      }
      try {
        const response = JSON.parse(stdout);
        if (response.error || !response.data) throw new Error("Wallet command failed.");
        resolve(response.data);
      } catch {
        reject(new Error("Wallet command failed."));
      }
    });
  });
}

async function ask(text) {
  const input = createInterface({ input: process.stdin, output: process.stderr });
  try {
    return (await input.question(`${text}\nApprove? [y/N] `)).trim().toLowerCase() === "y";
  } finally {
    input.close();
  }
}

async function withFiles(callback) {
  const base = await mkdtemp(join(tmpdir(), "xian-wallet-wc-"));
  try {
    return await callback((name) => join(base, name));
  } finally {
    await rm(base, { force: true, recursive: true });
  }
}

function checkIntent(intent) {
  if (!intent || (intent.chainId && intent.chainId !== chainId)) {
    throw new Error("The requested call is on a different network.");
  }
  if (!/^[A-Za-z][A-Za-z0-9_]{0,127}$/.test(intent.contract)
      || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(intent.function)
      || typeof intent.contract !== "string" || typeof intent.function !== "string") {
    throw new Error("The requested contract call is invalid.");
  }
  return intent;
}

async function draftIntent(intent, file) {
  checkIntent(intent);
  const argFile = file("args.json");
  await writeFile(argFile, JSON.stringify(intent.kwargs ?? {}), { mode: 0o600 });
  const args = ["prepare-call", "--from", address, "--contract", intent.contract,
    "--function", intent.function, "--args", argFile, "--out", file("proposal.json")];
  const chi = intent.chiSupplied ?? intent.chi;
  if (chi != null) args.push("--chi-limit", String(chi));
  return cli(args);
}

async function draftPayload(payload, file) {
  if (payload?.chain_id !== chainId || payload.sender !== address) {
    throw new Error("Transaction chain or sender does not match the session.");
  }
  await writeFile(file("payload.json"), JSON.stringify(payload), { mode: 0o600 });
  return cli(["prepare-payload", file("payload.json"), "--out", file("proposal.json")]);
}

async function signTransaction(draft, file, broadcast) {
  const proof = file("approval.json");
  await cli(["approve", file("proposal.json"), "--account", accountName, "--out", proof, "--textual"], true);
  if (!broadcast) {
    const { transaction_signature } = JSON.parse(await readFile(proof, "utf8"));
    return { payload: draft.payload, metadata: { signature: transaction_signature } };
  }
  const result = await cli(["submit", file("proposal.json"), "--approval", proof]);
  return {
    txHash: result.tx_hash,
    accepted: result.status === "completed" || result.status === "pending",
    finalized: result.status === "completed" || result.status === "failed",
    receipt: result.status === "pending" ? null
      : { success: result.status === "completed", txHash: result.tx_hash, message: result.message },
  };
}

async function handle({ method, params }) {
  const fields = Array.isArray(params) ? params[0] : params;
  switch (method) {
    case "xian_accounts":
    case "xian_requestAccounts":
      return [address];
    case "xian_chainId":
      return chainId;
    case "xian_getWalletInfo":
      return { accounts: [address], selectedAccount: address, chainId, connected: true,
        locked: true, capabilities: { getWalletInfo: true, prepareTransaction: true,
          signMessage: true, signTransaction: true, sendTransaction: true,
          sendCall: true, switchChain: false, watchAsset: false } };
    case "xian_signMessage":
      if (typeof fields?.message !== "string") throw new Error("A UTF-8 message is required.");
      return withFiles(async (file) => {
        await writeFile(file("message.txt"), fields.message, { mode: 0o600 });
        await cli(["message", "sign", file("message.txt"), "--account", accountName, "--out", file("signed.json")], true);
        return JSON.parse(await readFile(file("signed.json"), "utf8")).signature;
      });
    case "xian_prepareTransaction":
      return withFiles(async (file) => ({ payload: (await draftIntent(fields?.intent, file)).payload }));
    case "xian_signTransaction":
    case "xian_sendTransaction":
      return withFiles(async (file) => signTransaction(
        await draftPayload(fields?.tx?.payload ?? fields?.tx, file), file, method === "xian_sendTransaction",
      ));
    case "xian_sendCall":
      return withFiles(async (file) => signTransaction(await draftIntent(fields?.intent, file), file, true));
    default:
      throw new Error("WalletConnect method is unavailable.");
  }
}

kit.on("session_proposal", ({ id, params }) => serial(async () => {
  try {
    const namespaces = reviewedNamespaces(params.requiredNamespaces, chain, address);
    const peer = JSON.stringify(params.proposer.metadata.url ?? "").slice(0, 300);
    const text = `WalletConnect ${peer}\nChain ${chain}\nAccount ${address}\nMethods ${namespaces.xian.methods.join(", ")}`;
    if (!(await ask(text))) throw new Error("Connection declined.");
    await kit.approveSession({ id, namespaces });
  } catch {
    await kit.rejectSession({ id, reason: { code: 5000, message: "Rejected by the wallet." } });
  }
}));

kit.on("session_request", ({ id, topic, params }) => serial(async () => {
  const session = kit.getActiveSessions()[topic];
  const allowed = permits(session, chain, address, params.chainId, params.request.method);
  const response = { jsonrpc: "2.0", id };
  try {
    if (!allowed) throw new Error("The request exceeds the permitted session scope.");
    if (["xian_signMessage", "xian_signTransaction", "xian_sendCall", "xian_sendTransaction"].includes(params.request.method)) {
      const peer = JSON.stringify(session.peer?.metadata?.url ?? "").slice(0, 300);
      process.stderr.write("WalletConnect request from " + peer + " via " + params.request.method + "\n");
    }
    response.result = await handle(params.request);
  } catch {
    response.error = { code: 5000, message: "Wallet did not authorize or complete the request." };
  }
  await kit.respondSessionRequest({ topic, response });
}));

process.on("SIGINT", async () => {
  for (const topic of Object.keys(kit.getActiveSessions())) {
    await kit.disconnectSession({ topic, reason: { code: 6000, message: "Wallet session closed." } }).catch(() => {});
  }
  process.exit(0);
});

await kit.pair({ uri });
process.stderr.write("WalletConnect pairing active. Press Ctrl+C to close the session.\n");
