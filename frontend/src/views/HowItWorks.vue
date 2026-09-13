<script setup>
// Explainer page — the product's own pitch deck. Doubles as the demo
// video's narrative backbone: each section maps to a video beat.
import AppHeader from '../components/AppHeader.vue'

const pipeline = [
  { step: '01', name: 'Split days', en: 'Days are split across cities proportionally to the spots you picked — city blocks stay contiguous.' },
  { step: '02', name: 'Group by district', en: 'A greedy nearest-neighbour chain walks spot to spot, then cuts into day-sized segments. One day = one district, by construction.' },
  { step: '03', name: 'Pack to capacity', en: 'Each day carries a ~9-hour budget: stays + lunch + dinner + calibrated transit hops. Overflow moves, it never piles up.' },
  { step: '04', name: 'Anchor the hotel', en: 'One hostel per city, chosen against the trip’s geographic centre — every day starts and ends at your door.' },
  { step: '05', name: 'Check closing times', en: 'The plan is replayed against real opening hours. Anything unreachable is rescued: reordered, moved across days, or honestly flagged.' },
  { step: '06', name: 'Time the clock', en: 'A clock replay with real transit minutes produces the timetable — buses numbered, fares in USD, dinner at 18:00.' },
]

const agentTools = [
  { glyph: 'search_pois', en: 'Reads the real spot catalog — 328 entries with opening hours, stay times, tickets. Nothing is invented.' },
  { glyph: 'draft_day_plan', en: 'Calls the classic engine for grouping, hotels and feasibility. The agent asks; the engine decides.' },
  { glyph: 'replay_clock', en: 'Replays a day against real transit to get true visit times — the times you see are engine times, not model guesses.' },
  { glyph: 'transit_route', en: 'Real cached Google-Maps transit legs (SerpApi), with a calibrated estimator as the honest fallback.' },
  { glyph: 'trip_budget', en: 'Adds up transit, tickets and stays per day — the arithmetic on screen was computed, not asserted.' },
  { glyph: 'ask_traveller', en: 'The only gate where the agent stops and asks you: a genuine decision, with concrete options. Everything else it handles silently.' },
]

const values = [
  {
    title: 'Plan once, guarded monthly',
    en: 'A trip is not a document — it’s a living thing. The watcher keeps replaying your booked days against reality and pushes one notification when something breaks, not a wall of warnings. That is a subscription, not a one-shot tool.',
  },
  {
    title: 'Trust as the product',
    en: 'Every time, hotel and fare on screen is engine-computed from real data — the model orchestrates, it never fabricates. In a market drowning in AI hallucinations, an itinerary that can’t lie is the moat.',
  },
  {
    title: 'The engine is sellable alone',
    en: 'The deterministic planner behind both modes is a standalone API: hostels, DMOs and OTAs could license “feasible, closable, transit-only day plans” without shipping any LLM at all.',
  },
]
</script>

<template>
  <div class="how-page">
    <AppHeader tagline="the story behind the two modes" />

    <div class="how-scroll">
      <main class="how-main">

        <!-- ============ HERO ============ -->
        <section class="hero">
          <p class="kicker">AlonTrip · East Asia, public transit only</p>
          <h1>Two ways to plan a trip.<br/><em>The engine never changes.</em></h1>
          <p class="lede">
            AlonTrip began as a form: pick a city, pick spots, get a
            transit-perfect day plan. It grew into an agent: describe the trip,
            and a Strands-powered concierge plans it with you — pausing only
            for the decisions that are truly yours. Both modes share one
            deterministic planning engine, so what the agent promises, the
            engine can actually deliver.
          </p>
          <div class="hero-chain">
            <span class="node hotel">hotel</span>
            <span class="link"></span>
            <span class="node">spot</span>
            <span class="link"></span>
            <span class="node">spot</span>
            <span class="link"></span>
            <span class="node hotel">hotel</span>
            <span class="chain-note">every day, both modes: start at your door, end at your door</span>
          </div>
        </section>

        <!-- ============ WHY ============ -->
        <section class="sec">
          <div class="sec-head"><span class="no">01</span><h2>Why this exists</h2></div>
          <div class="cols">
            <p class="body">
              Budget travel across Japan, Korea and China runs on subways,
              buses and trains — but every planning tool assumes a car or
              happily invents walking times. A backpacker landing at Kansai at
              15:30 needs to know <em>which</em> of their spots are still
              reachable before closing, in what order, on which lines, for how
              much — and whether they should sleep near Gion or Kyoto Station.
            </p>
            <p class="body">
              That question is answered by arithmetic against real data:
              opening hours, transit minutes, fares, day capacity. AlonTrip
              was built as exactly that arithmetic — no narration, no
              hand-waving, a timetable you can trust.
            </p>
          </div>
        </section>

        <!-- ============ CLASSIC ============ -->
        <section class="sec">
          <div class="sec-head"><span class="no">02</span><h2>Classic mode — the deterministic engine</h2></div>
          <p class="body intro">
            You pick a city, the days and the spots. The engine does the rest
            in six passes — the same six passes the agent later stands on:
          </p>
          <ol class="pipeline">
            <li v-for="p in pipeline" :key="p.step">
              <span class="p-no">{{ p.step }}</span>
              <div>
                <h3>{{ p.name }}</h3>
                <p>{{ p.en }}</p>
              </div>
            </li>
          </ol>
          <p class="body footnote">
            Real transit comes from SerpApi’s Google-Maps directions
            (cached — the same leg is never paid for twice), fares normalised
            to USD.
          </p>
        </section>

        <!-- ============ WHY AGENT ============ -->
        <section class="sec">
          <div class="sec-head"><span class="no">03</span><h2>Why an agent — and why now</h2></div>
          <div class="cols">
            <p class="body">
              Classic mode answers “given these spots, what’s the best
              plan?” But travel planning starts earlier and messier:
              “four days in Tokyo, Disneyland is a must, we land in the
              afternoon.” Turning that sentence into a spot list, a hotel
              and a feasible timetable was exactly the kind of open-ended
              reasoning agents are good at.
            </p>
            <p class="body">
              The timing matched: AWS’ <strong>Agents for Humans</strong>
              hackathon asked for agents built on the open-source
              <strong>Strands Agents SDK</strong> that “run autonomously
              and only surface when there’s a real decision.”
              That thesis — <em>the agent works, you keep the pen</em> — was
              already this product’s honest-by-construction personality.
              So the agent mode was born: the LLM finally allowed in, but
              only ever as the front door to the engine.
            </p>
          </div>
        </section>

        <!-- ============ AGENT ============ -->
        <section class="sec">
          <div class="sec-head"><span class="no">04</span><h2>Agent mode — the model orchestrates, the engine decides</h2></div>
          <p class="body intro">
            A Strands agent loop (tools in, structured plan out) drives six
            tools. Four of them are the classic engine itself:
          </p>
          <ul class="tools">
            <li v-for="t in agentTools" :key="t.glyph">
              <code>⏺ {{ t.glyph }}</code>
              <p>{{ t.en }}</p>
            </li>
          </ul>
          <div class="callout">
            <p class="call-head">The rule that makes it trustworthy</p>
            <p class="body">
              The itinerary you see is never the model’s wording. After the
              conversation settles, the server <em>re-runs the engine</em> with
              the agent’s chosen inputs and rebuilds the plan from
              planner.py’s output — hotels, day counts, spot sets, clock
              times. If the model drifts, the rebuild silently corrects it.
              A plan that “human-authored” feel with zero
              hallucination surface.
            </p>
          </div>
          <div class="callout alt">
            <p class="call-head">After booking: the watcher</p>
            <p class="body">
              A deterministic watcher (no LLM) re-replays booked days when
              reality changes — “Kiyomizu-dera closes at 08:30 today”
              — tries the smallest fix first, and pushes <em>one</em>
              notification: what changed, or the single decision left for you.
            </p>
          </div>
        </section>

        <!-- ============ VALUE ============ -->
        <section class="sec">
          <div class="sec-head"><span class="no">05</span><h2>Where the value lands</h2></div>
          <div class="value-grid">
            <article v-for="v in values" :key="v.title">
              <h3>{{ v.title }}</h3>
              <p>{{ v.en }}</p>
            </article>
          </div>
        </section>

        <!-- ============ ARCHITECTURE ============ -->
        <section class="sec">
          <div class="sec-head"><span class="no">06</span><h2>The system, drawn honestly</h2></div>
          <p class="body intro">
            One process, four layers, zero hand-waving — data flows top to bottom:
          </p>

          <div class="arch">
            <!-- LAYER 1: browser -->
            <div class="layer l-browser">
              <span class="layer-tag">browser</span>
              <div class="boxes">
                <div class="box">Agent console<span class="sub">chat · plan · decision cards</span></div>
                <div class="box">Classic form<span class="sub">cities · spots · hubs · flights</span></div>
                <div class="box">Map<span class="sub">routes · hotel pins · arrows</span></div>
              </div>
            </div>
            <div class="flow"><span>SSE stream / JSON</span></div>

            <!-- LAYER 2: api -->
            <div class="layer l-api">
              <span class="layer-tag">FastAPI · :5003</span>
              <div class="boxes">
                <div class="box gold">/agent/chat · /agent/resume<span class="sub">Strands agent loop, streamed</span></div>
                <div class="box">/optimize-route<span class="sub">classic six-pass engine</span></div>
                <div class="box">/transit-legs<span class="sub">map fine-tuning</span></div>
              </div>
            </div>
            <div class="flow"><span>tool calls (agent) · direct calls (classic)</span></div>

            <!-- LAYER 3: agent + engine -->
            <div class="layer-split">
              <div class="layer l-agent">
                <span class="layer-tag">Strands agent</span>
                <div class="boxes">
                  <div class="box">6 tools<span class="sub">search · draft · replay · transit · budget · ask</span></div>
                  <div class="box">interrupts<span class="sub">⏸ human decisions only</span></div>
                </div>
              </div>
              <div class="layer l-engine">
                <span class="layer-tag">deterministic engine</span>
                <div class="boxes">
                  <div class="box">planner.py<span class="sub">NN-chain · capacity · closing check</span></div>
                  <div class="box">schedule.py<span class="sub">clock replay · meals · caps</span></div>
                  <div class="box">hours.py<span class="sub">328-spot opening-hours parser</span></div>
                </div>
              </div>
            </div>
            <div class="flow"><span>reads / writes</span></div>

            <!-- LAYER 4: data -->
            <div class="layer l-data">
              <span class="layer-tag">data</span>
              <div class="boxes">
                <div class="box">spot catalog<span class="sub">328 POIs · 9 cities</span></div>
                <div class="box">lodgings<span class="sub">hostels per city</span></div>
                <div class="box">transit cache<span class="sub">real Google-Maps legs</span></div>
                <div class="box">SerpApi<span class="sub">live legs, agent mode only</span></div>
              </div>
            </div>
          </div>

          <div class="callout side">
            <p class="call-head">The trust boundary</p>
            <p class="body">
              The model lives <em>only</em> in the Strands layer. Everything below it
              is arithmetic on real data — which is why the itinerary can be
              rebuilt deterministically after every conversation turn.
            </p>
          </div>
        </section>

        <!-- ============ CTA ============ -->
        <section class="cta">
          <p class="cta-line">Try both doors to the same engine —</p>
          <div class="cta-row">
            <RouterLink to="/" class="cta-btn gold">Plan by conversation</RouterLink>
            <RouterLink to="/classic" class="cta-btn">Plan by form</RouterLink>
          </div>
          <p class="cta-sub">alonuniverse.com/trip · live</p>
        </section>

      </main>
    </div>
  </div>
</template>

<style scoped>
.how-page {
  height: 100vh;
  display: flex;
  flex-direction: column;
  padding: 14px 22px 0;
  gap: 12px;
  overflow: hidden;
}
.how-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  scrollbar-width: thin;
}
.how-main {
  max-width: 780px;
  margin: 0 auto;
  padding: 30px 8px 90px;
}

/* ---------- hero ---------- */
.kicker {
  font-size: 11px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--gold);
  margin-bottom: 14px;
}
.hero h1 {
  font-size: clamp(30px, 4.2vw, 46px);
  line-height: 1.12;
  font-weight: 750;
  letter-spacing: -0.015em;
  text-wrap: balance;
}
.hero h1 em {
  font-style: normal;
  color: var(--gold);
}
.lede {
  margin-top: 18px;
  font-size: 15.5px;
  line-height: 1.75;
  color: var(--text);
  max-width: 62ch;
  text-wrap: pretty;
}
.hero-chain {
  margin-top: 30px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.node {
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 5px 14px;
  font-size: 12px;
  color: var(--muted);
  background: var(--panel);
}
.node.hotel {
  border-color: var(--gold);
  color: var(--gold);
}
.link {
  width: 26px;
  height: 1px;
  background: var(--border);
  position: relative;
}
.link::after {
  content: '';
  position: absolute;
  right: 0;
  top: -2.5px;
  border-left: 5px solid var(--border);
  border-top: 3px solid transparent;
  border-bottom: 3px solid transparent;
}
.chain-note {
  flex-basis: 100%;
  margin-top: 8px;
  font-size: 11.5px;
  color: var(--muted);
  letter-spacing: 0.05em;
}

/* ---------- sections ---------- */
.sec {
  margin-top: 72px;
}
.sec-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 14px;
}
.no {
  font-size: 12px;
  color: var(--gold);
  letter-spacing: 0.1em;
  font-weight: 700;
}
.sec-head h2 {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.body {
  font-size: 14.5px;
  line-height: 1.8;
  color: var(--text);
  text-wrap: pretty;
}
.body em { color: var(--gold); font-style: normal; }
.cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 26px;
}
@media (max-width: 760px) { .cols { grid-template-columns: 1fr; } }
.intro { max-width: 62ch; margin-bottom: 24px; color: var(--muted); }
.footnote { margin-top: 22px; color: var(--muted); font-size: 13px; }

/* pipeline */
.pipeline {
  list-style: none;
  display: grid;
  gap: 0;
}
.pipeline li {
  display: flex;
  gap: 18px;
  padding: 16px 4px;
  border-bottom: 1px dashed var(--border);
}
.pipeline li:last-child { border-bottom: none; }
.p-no {
  font-size: 12px;
  color: var(--gold);
  font-weight: 700;
  letter-spacing: 0.08em;
  padding-top: 3px;
  min-width: 22px;
}
.pipeline h3 {
  font-size: 14.5px;
  font-weight: 650;
  margin-bottom: 4px;
}
.pipeline p {
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--muted);
  text-wrap: pretty;
}

/* tools */
.tools {
  list-style: none;
  display: grid;
  gap: 12px;
}
.tools li {
  display: flex;
  gap: 14px;
  align-items: baseline;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
}
.tools code {
  font-size: 12px;
  color: #56d4dd;
  white-space: nowrap;
  font-family: ui-monospace, Menlo, monospace;
}
.tools p {
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--muted);
  text-wrap: pretty;
}

/* callouts */
.callout {
  margin-top: 26px;
  border-left: 2px solid var(--gold);
  padding: 16px 20px;
  background: linear-gradient(90deg, rgba(255, 209, 102, 0.05), transparent 70%);
}
.callout.alt {
  border-left-color: #56d4dd;
  background: linear-gradient(90deg, rgba(86, 212, 221, 0.05), transparent 70%);
}
.call-head {
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--gold);
  margin-bottom: 8px;
  font-weight: 700;
}
.callout.alt .call-head { color: #56d4dd; }

/* value */
.value-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}
@media (max-width: 860px) { .value-grid { grid-template-columns: 1fr; } }
.value-grid article {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px;
  background: var(--panel);
}
.value-grid h3 {
  font-size: 15px;
  font-weight: 650;
  margin-bottom: 10px;
  color: var(--gold);
}
.value-grid p {
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--muted);
  text-wrap: pretty;
}

/* architecture diagram */
.arch { display: flex; flex-direction: column; gap: 0; }
.layer {
  position: relative;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel);
  padding: 14px 16px 16px 92px;
}
.layer-tag {
  position: absolute;
  left: 14px; top: 14px;
  writing-mode: vertical-rl;
  transform: rotate(180deg);
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--muted);
  border-left: 1px solid var(--border);
  padding-left: 6px;
  max-height: calc(100% - 28px);
}
.boxes { display: flex; gap: 10px; flex-wrap: wrap; }
.box {
  flex: 1 1 150px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 13px;
  font-weight: 600;
  background: var(--bg);
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.box .sub { font-size: 11px; color: var(--muted); font-weight: 400; }
.box.gold { border-color: var(--gold); }
.flow {
  display: flex; align-items: center; justify-content: center;
  height: 34px; position: relative;
}
.flow::before {
  content: '';
  position: absolute; left: 50%; top: 0; bottom: 0;
  width: 1px; background: var(--border);
}
.flow::after {
  content: '';
  position: absolute; left: calc(50% - 4px); bottom: 0;
  border-top: 5px solid var(--border);
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
}
.flow span {
  position: relative;
  background: var(--bg);
  padding: 0 10px;
  font-size: 10.5px;
  color: var(--muted);
  letter-spacing: 0.06em;
}
.layer-split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 760px) { .layer-split { grid-template-columns: 1fr; } }
.layer { padding-left: 84px; }
.callout.side { margin-top: 24px; border-left-color: var(--gold); }

/* cta */
.cta {
  margin-top: 90px;
  text-align: center;
}
.cta-line {
  font-size: 16px;
  color: var(--text);
}
.cta-row {
  margin-top: 20px;
  display: flex;
  gap: 14px;
  justify-content: center;
}
.cta-btn {
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 12px 26px;
  font-size: 14px;
  color: var(--text);
  text-decoration: none;
}
.cta-btn.gold {
  background: var(--gold);
  border-color: var(--gold);
  color: #1c1400;
  font-weight: 700;
}
.cta-btn:hover { border-color: var(--gold); }
.cta-sub {
  margin-top: 18px;
  font-size: 11.5px;
  color: var(--muted);
  letter-spacing: 0.08em;
}
</style>
