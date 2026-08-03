"use client";

import { useState } from "react";

const GITHUB = "https://github.com/AbsoluteMode/session-recall";

type ToolExample = {
  name: string;
  kicker: string;
  description: string;
  input: string;
  output: string[];
};

const tools: ToolExample[] = [
  {
    name: "recall_search",
    kicker: "Find the memory",
    description:
      "Search by meaning across every agent, with optional project, source, and date boundaries.",
    input: `recall_search({
  query: "why did refresh tokens conflict?",
  scope_cwd: "/work/keeper"
})`,
    output: [
      "source  codex",
      "when    3 months ago",
      "score   0.914",
      "hit     One OAuth account rotated the token",
      "         out from under the other service…",
    ],
  },
  {
    name: "expand_around",
    kicker: "Open the evidence",
    description:
      "Turn a ranked anchor into the raw conversation around it — including tools, outputs, and reasoning.",
    input: `expand_around({
  session_id: "keeper-rollout",
  uuid: "oauth-rotation",
  before: 2,
  after: 3
})`,
    output: [
      "user       both workers use the same account",
      "assistant  the provider rotates on every refresh",
      "tool       read auth/session_store.py",
      "result     both services persist independently",
      "assistant  each refresh invalidates its peer",
    ],
  },
  {
    name: "step",
    kicker: "Follow the thread",
    description:
      "Walk forward or backward from a known turn without paying for another semantic search.",
    input: `step({
  session_id: "keeper-rollout",
  uuid: "oauth-rotation",
  direction: "next",
  count: 2
})`,
    output: [
      "next 1  rejected shared credential directory",
      "        too coupled to the deployment layout",
      "next 2  chose one keeper service to own refresh",
      "        follow-up: write the missing spec",
    ],
  },
  {
    name: "grep",
    kicker: "Find the exact trace",
    description:
      "Scan raw transcripts for an identifier or error that never made it into an assistant answer.",
    input: `grep({
  pattern: "invalid_grant",
  scope_cwd: "/work/keeper",
  source: "claude"
})`,
    output: [
      "tool_result  POST /oauth/token → 400",
      "error        invalid_grant: token already used",
      "session      auth-regression-2026-05-14",
      "provenance   claude · tool output",
    ],
  },
  {
    name: "recent_sessions",
    kicker: "See what is current",
    description:
      "List the freshest work first, including resumed sessions that belong to the same arc.",
    input: `recent_sessions({
  scope_cwd: "/work/keeper",
  source: "cursor",
  limit: 3
})`,
    output: [
      "2m ago   cursor  verify keeper failover",
      "1d ago   cursor  add token rotation fixture",
      "3d ago   cursor  trace invalid_grant recurrence",
      "freshness       index is up to date",
    ],
  },
];

const hostInstalls = [
  {
    name: "Claude Code",
    tag: "native plugin",
    command:
      "/plugin marketplace add AbsoluteMode/session-recall\n/plugin install session-recall",
    note: "Start a new session, then ask: “what did we decide last time?”",
  },
  {
    name: "Codex",
    tag: "native plugin",
    command:
      ".codex-plugin/plugin.json\n# install from the repository as a local plugin",
    note: "Review the bundled SessionStart hook once in /hooks.",
  },
  {
    name: "Cursor",
    tag: "native plugin",
    command:
      "cursor-agent plugin marketplace add https://github.com/AbsoluteMode/session-recall.git\n/add-plugin session-recall",
    note: "Approve the local session-recall MCP server on first use.",
  },
];

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <button className="copyButton" type="button" onClick={copy}>
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

export default function Home() {
  const [activeTool, setActiveTool] = useState(0);
  const tool = tools[activeTool];
  const install =
    "pipx install git+https://github.com/AbsoluteMode/session-recall\nsession-recall setup";

  return (
    <main>
      <nav className="nav shell" aria-label="Primary navigation">
        <a className="brand" href="#top" aria-label="Session Recall home">
          <img className="brandLogo" src="/logo.png" alt="" aria-hidden="true" />
          <span>session-recall</span>
        </a>
        <div className="navLinks">
          <a href="#tools">Tools</a>
          <a href="#team">Team</a>
          <a href="#install">Install</a>
          <a href="#privacy">Privacy</a>
          <a className="navGit" href={GITHUB} target="_blank" rel="noreferrer">
            GitHub <span aria-hidden="true">↗</span>
          </a>
        </div>
      </nav>

      <section className="hero shell" id="top">
        <div className="heroCopy">
          <div className="eyebrow">
            <span className="pulse" aria-hidden="true" />
            Open source · local-first · MCP native
          </div>
          <h1>
            Your coding agents forget.
            <span>Session Recall doesn’t.</span>
          </h1>
          <p className="heroLead">
            One searchable memory for Claude Code, Codex, and Cursor. Find an old
            decision by meaning, open the raw evidence, and continue exactly where
            the work stopped.
          </p>
          <div className="heroActions">
            <a className="button primary" href="#install">
              Install in two minutes <span aria-hidden="true">→</span>
            </a>
            <a className="button secondary" href={GITHUB} target="_blank" rel="noreferrer">
              View source <span aria-hidden="true">↗</span>
            </a>
          </div>
          <div className="heroProof" aria-label="Project highlights">
            <span><b>3</b> histories</span>
            <span><b>5</b> recall tools</span>
            <span><b>0</b> keys required</span>
          </div>
        </div>

        <div className="memoryWindow" aria-label="A memory moving between coding agents">
          <div className="windowBar">
            <div className="windowDots" aria-hidden="true"><i /><i /><i /></div>
            <span>memory://keeper/auth</span>
            <span className="liveState">LIVE INDEX</span>
          </div>
          <div className="agentRail" aria-label="Indexed sources">
            <div><span className="sourceIcon claude">C</span><b>Claude Code</b><small>May 14</small></div>
            <div><span className="sourceIcon codex">X</span><b>Codex</b><small>Jun 02</small></div>
            <div><span className="sourceIcon cursor">›_</span><b>Cursor</b><small>now</small></div>
          </div>
          <div className="memoryQuery">
            <span className="prompt">you</span>
            <p>Where did we land on the refresh-token conflict?</p>
          </div>
          <div className="recallTrace">
            <div className="traceHead">
              <span><i className="tracePulse" /> recall_search</span>
              <b>0.914</b>
            </div>
            <p>
              Both services shared one OAuth account. Rotation made each refresh
              invalidate the other worker’s copy.
            </p>
            <div className="traceMeta">
              <span>codex</span><span>keeper</span><span>3 months ago</span>
            </div>
          </div>
          <div className="decisionCard">
            <span>decision recovered</span>
            <p>One keeper service owns the session. The spec was the next step.</p>
          </div>
          <div className="scanline" aria-hidden="true" />
        </div>
      </section>

      <section className="sourceStrip" aria-label="Supported coding agents">
        <div className="shell sourceStripInner">
          <span>THE SAME MEMORY, WHEREVER YOU WORK</span>
          <div><b>Claude Code</b><i /> <b>Codex</b><i /> <b>Cursor</b><i /> <b>any MCP client</b></div>
        </div>
      </section>

      <section className="problem shell section" id="why">
        <div className="sectionLabel">01 / THE GAP</div>
        <div className="problemGrid">
          <h2>Your work has continuity.<br />Your chat windows don’t.</h2>
          <div className="problemCopy">
            <p>
              The fix from May, the constraint you rejected in June, the tool output
              that proved it — all of it still exists. It is simply buried across
              agents, subscriptions, worktrees, and resumed sessions.
            </p>
            <p>
              Session Recall indexes the actual conversation surface and keeps the
              deeper trace one step away. No summary file to maintain. No copy-paste
              ritual at the beginning of every session.
            </p>
          </div>
        </div>
        <div className="payoffGrid">
          <article><span>01</span><h3>Resume old work</h3><p>Recover the decision, rejected alternatives, and unfinished next step.</p></article>
          <article><span>02</span><h3>Catch regressions</h3><p>Ask whether a bug happened before — and why the previous fix looked correct.</p></article>
          <article><span>03</span><h3>Cross agent boundaries</h3><p>Let Cursor continue what Codex discovered and Claude Code validated.</p></article>
          <article><span>04</span><h3>Replay procedures</h3><p>Explain an operational workflow once. Retrieve the grounded steps later.</p></article>
        </div>
      </section>

      <section className="toolSection section" id="tools">
        <div className="shell">
          <div className="sectionLabel light">02 / THE TOOLKIT</div>
          <div className="toolIntro">
            <h2>Search wide.<br />Then go deep.</h2>
            <p>
              Five small MCP tools form a retrieval workflow. Start with an anchor,
              inspect the evidence, and move through the original session without
              stuffing the whole archive into context.
            </p>
          </div>

          <div className="toolLab">
            <div className="toolTabs" role="tablist" aria-label="Recall tools">
              {tools.map((item, index) => (
                <button
                  key={item.name}
                  type="button"
                  role="tab"
                  aria-selected={activeTool === index}
                  aria-controls="tool-panel"
                  className={activeTool === index ? "active" : ""}
                  onClick={() => setActiveTool(index)}
                >
                  <span>0{index + 1}</span>{item.name}
                </button>
              ))}
            </div>
            <div className="toolPanel" id="tool-panel" role="tabpanel">
              <div className="toolPanelCopy">
                <span>{tool.kicker}</span>
                <h3>{tool.name}</h3>
                <p>{tool.description}</p>
              </div>
              <div className="codePane">
                <div className="codeHeader"><span>agent call</span><CopyButton value={tool.input} /></div>
                <pre><code>{tool.input}</code></pre>
                <div className="resultPane">
                  <span className="resultLabel">grounded result</span>
                  {tool.output.map((line) => <div key={line}>{line}</div>)}
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="architecture shell section" id="privacy">
        <div className="sectionLabel">03 / BUILT LOCAL-FIRST</div>
        <div className="architectureHead">
          <h2>Your history stays yours.</h2>
          <p>
            The index, raw transcripts, Cursor snapshots, and retrieval all live on
            your machine. The default embedding model runs locally too.
          </p>
        </div>
        <div className="architectureMap">
          <div className="mapSources">
            <span>Claude JSONL</span><span>Codex JSONL</span><span>Cursor SQLite</span>
          </div>
          <div className="mapArrow"><span>incremental ingest</span></div>
          <div className="mapCore">
            <span className="coreMark">sr</span>
            <div><b>one local index</b><small>SQLite · KNN · FTS5</small></div>
          </div>
          <div className="mapArrow reverse"><span>on-demand MCP</span></div>
          <div className="mapAgents">
            <span>your active agent</span><b>grounded context</b>
          </div>
        </div>
        <div className="invariantGrid">
          <article><b>Raw stays local</b><p>Tool calls, outputs, and reasoning are never sent to an embedding provider.</p></article>
          <article><b>No key required</b><p>A bundled multilingual ONNX model makes the zero-config path fully local.</p></article>
          <article><b>Failure is honest</b><p>If semantic retrieval is unavailable, results say they degraded to literal matching.</p></article>
          <article><b>Sharing is explicit</b><p>Team answers are encrypted, project-scoped, secret-scanned, and owner-approved.</p></article>
        </div>
      </section>

      <section className="teamSection section" id="team">
        <div className="shell">
          <div className="sectionLabel light">04 / TEAM MEMORY</div>
          <div className="teamHead">
            <h2>Ask a teammate’s history.<br />Never take it.</h2>
            <div>
              <p>
                Pair once, then let your agent ask their agent how the team solved
                something before. Their raw sessions never leave their machine.
              </p>
              <p className="teamPromise">
                Only the project-scoped answer they approve comes back to you.
              </p>
            </div>
          </div>

          <div className="sharingFlow" aria-label="How team sharing works">
            <article className="peerCard requesterCard">
              <div className="peerHead">
                <span className="peerAvatar">YO</span>
                <div><b>Your agent</b><small>paired · keeper</small></div>
              </div>
              <div className="peerMessage">
                <span>question</span>
                <p>How did you fix the local launch?</p>
              </div>
              <small className="peerFoot">No access to their archive</small>
            </article>

            <div className="relayPath" aria-label="Encrypted blind relay">
              <span className="sealedBadge">SEALED</span>
              <div className="relayLine"><i /><i /><i /></div>
              <b>blind relay</b>
              <small>cannot read either message</small>
              <div className="relayLine return"><i /><i /><i /></div>
              <span className="approvedBadge">APPROVED ANSWER</span>
            </div>

            <article className="peerCard ownerCard">
              <div className="peerHead">
                <span className="peerAvatar owner">MK</span>
                <div><b>Teammate’s agent</b><small>local recall · keeper</small></div>
              </div>
              <div className="localChecks">
                <span><i>✓</i> project scope</span>
                <span><i>✓</i> secret scan</span>
                <span><i>✓</i> owner approval</span>
              </div>
              <small className="peerFoot">Raw history stays on this machine</small>
            </article>
          </div>

          <div className="sharingSteps">
            <article><span>01</span><b>Pair</b><p>Exchange one-time encrypted identities with a colleague.</p></article>
            <article><span>02</span><b>Allow</b><p>Grant read-only recall for one explicit project scope.</p></article>
            <article><span>03</span><b>Ask</b><p>Your agent sends a sealed question through your chosen transport.</p></article>
            <article><span>04</span><b>Approve</b><p>The owner reviews the answer before anything leaves their machine.</p></article>
          </div>

          <div className="transportNote">
            <span>NO CLOUD ACCOUNT REQUIRED</span>
            <p>Choose a shared folder or run your own relay. A fresh install has no sharing transport and sends nothing anywhere.</p>
          </div>
        </div>
      </section>

      <section className="installSection section" id="install">
        <div className="shell">
          <div className="sectionLabel light">05 / INSTALL</div>
          <div className="installHead">
            <h2>Two minutes to a memory<br />that spans every agent.</h2>
            <p>Install the CLI once. Then connect whichever coding agents you use.</p>
          </div>
          <div className="quickInstall">
            <div>
              <span>01 · install + index</span>
              <pre><code>{install}</code></pre>
            </div>
            <CopyButton value={install} />
          </div>
          <div className="hostGrid">
            {hostInstalls.map((host, index) => (
              <article key={host.name}>
                <div className="hostHead"><span>0{index + 2}</span><small>{host.tag}</small></div>
                <h3>{host.name}</h3>
                <pre><code>{host.command}</code></pre>
                <p>{host.note}</p>
              </article>
            ))}
          </div>
          <div className="verifyLine">
            <span>✓</span>
            <div><b>Verify the whole chain</b><code>session-recall health</code></div>
            <p>Freshness · embedder · vector space · corpus · sources</p>
          </div>
        </div>
      </section>

      <section className="finalCta shell section">
        <span className="sectionLabel">06 / REMEMBER THE WORK</span>
        <h2>Stop rebuilding context.<br /><em>Continue it.</em></h2>
        <p>Open source. Local-first. Built from real agent history.</p>
        <div className="heroActions center">
          <a className="button primary" href={GITHUB} target="_blank" rel="noreferrer">
            Get Session Recall <span aria-hidden="true">↗</span>
          </a>
          <a className="button secondary dark" href={`${GITHUB}#how-it-works`} target="_blank" rel="noreferrer">
            Read how it works
          </a>
        </div>
      </section>

      <footer>
        <div className="shell footerInner">
          <div className="brand"><img className="brandLogo" src="/logo.png" alt="" /><span>session-recall</span></div>
          <p>Shared memory for Claude Code, Codex, and Cursor.</p>
          <div><a href={GITHUB}>GitHub</a><a href={`${GITHUB}/blob/main/LICENSE`}>MIT License</a></div>
        </div>
      </footer>
    </main>
  );
}
