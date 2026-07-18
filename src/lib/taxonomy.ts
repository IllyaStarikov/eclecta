/**
 * The public taxonomy: six top categories, each with subcategories.
 *
 * Picks carry the pipeline's internal `channels[]` today; the front end derives
 * a single PRIMARY `category` plus cross-cutting `subcategories` from the title
 * lexicon (with the legacy channel slugs as a fallback signal). When the
 * pipeline emits `category`/`subcategories` natively, `deriveCategory` becomes
 * a fallback and the same tree still drives routing, nav, sections, and feeds.
 *
 * Keep the lexicon in sync with signalpipe/topics.py TAXONOMY.
 */
export interface Subcategory { slug: string; name: string; blurb: string; match: string[]; }
export interface Category {
  slug: string;
  name: string;
  blurb: string;
  match: string[];
  subcategories: Subcategory[];
}

export const CATEGORIES: Category[] = [
  {
    slug: 'ai',
    name: 'AI',
    blurb: 'Frontier and open-weight models, agents, the labs, and the policy around them.',
    match: ['artificial intelligence', ' ai ', 'a.i.', 'llm', 'machine learning', 'neural net', 'openai', 'anthropic', 'deepmind', 'gpt', 'claude', 'gemini', 'llama', 'mistral', 'chatbot'],
    subcategories: [
      { slug: 'models', name: 'Models', blurb: 'Frontier and open-weight model releases and what they can do.', match: ['frontier model', 'open-weight', 'open weight', 'foundation model', 'multimodal', 'model release', 'context window', 'parameters', 'gpt-', 'llama ', 'mixture of experts'] },
      { slug: 'agents', name: 'Agents', blurb: 'Agentic systems, tool use, and orchestration.', match: ['agent', 'agentic', 'tool use', 'tool-use', 'mcp', 'autonomy', 'autonomous', 'orchestrat', 'agent loop'] },
      { slug: 'evals', name: 'Evals', blurb: 'Benchmarks, evaluations, and leaderboards.', match: ['benchmark', 'eval', 'leaderboard', 'mmlu', 'arena', 'state-of-the-art', 'sota', 'pass@'] },
      { slug: 'safety', name: 'Safety & policy', blurb: 'Alignment, interpretability, and AI governance.', match: ['alignment', 'interpretab', 'rlhf', 'jailbreak', 'red team', 'red-team', 'ai safety', 'ai policy', 'guardrail', 'model welfare', 'refus'] },
      { slug: 'apps', name: 'Applied AI', blurb: 'AI in products: assistants, RAG, inference.', match: ['copilot', 'assistant', 'rag', 'retrieval-augmented', 'inference', 'genai', 'generative ai', 'prompt'] },
    ],
  },
  {
    slug: 'research',
    name: 'Research',
    blurb: 'Papers and results that change practice, from machine learning to the lab bench.',
    match: ['paper', 'arxiv', 'study', 'researchers', 'preprint', 'journal', 'findings', 'experiment'],
    subcategories: [
      { slug: 'ml', name: 'ML & methods', blurb: 'Machine-learning methods and results.', match: ['transformer', 'diffusion', 'reinforcement learning', 'gradient', 'fine-tun', 'embedding', 'neural architecture', 'self-supervised', 'dataset'] },
      { slug: 'systems', name: 'Systems & theory', blurb: 'Systems, algorithms, and theory.', match: ['algorithm', 'complexity', 'distributed system', 'consensus', 'theory', 'random graph', 'data structure', 'formal verification'] },
      { slug: 'science', name: 'Sciences', blurb: 'Physics, biology, chemistry, and the rest.', match: ['physics', 'quantum', 'biology', 'genom', 'chemistry', 'astronom', 'climate', 'neuroscience', 'materials science', 'particle', 'protein', 'fusion'] },
      { slug: 'math', name: 'Mathematics', blurb: 'Mathematics and its frontiers.', match: ['mathematic', 'theorem', 'conjecture', 'number theory', 'topology', 'combinatoric', 'prime'] },
    ],
  },
  {
    slug: 'software',
    name: 'Software',
    blurb: 'Languages, systems, and the craft of building.',
    match: ['programming', 'open source', 'open-source', 'library', 'framework', 'developer', ' api ', 'codebase'],
    subcategories: [
      { slug: 'languages', name: 'Languages', blurb: 'Languages, compilers, and runtimes.', match: ['rust', 'python', 'golang', 'typescript', 'javascript', 'c++', 'compiler', 'language', 'runtime', 'wasm', 'webassembly', 'zig'] },
      { slug: 'data', name: 'Data', blurb: 'Databases, data systems, and pipelines.', match: ['database', 'sql', 'postgres', 'sqlite', 'data pipeline', 'warehouse', 'duckdb', 'kafka', 'query engine'] },
      { slug: 'infra', name: 'Infrastructure', blurb: 'Infra, cloud, and operations.', match: ['kubernetes', 'docker', 'cloud', 'serverless', 'devops', 'observability', 'infrastructure', 'deployment', 'terraform'] },
      { slug: 'web', name: 'Web', blurb: 'The web platform and frontend.', match: ['browser', ' css', 'html', 'frontend', 'react', 'web platform', 'dom ', 'http'] },
      { slug: 'practice', name: 'Practice', blurb: 'Engineering practice and craft.', match: ['testing', 'refactor', 'code review', 'technical debt', 'architecture', 'postmortem', 'best practice', 'maintainab'] },
    ],
  },
  {
    slug: 'security',
    name: 'Security',
    blurb: 'Vulnerabilities, research, and the adversarial edge.',
    match: ['security', 'vulnerab', 'exploit', 'malware', 'breach', 'hacked', 'cve', 'ransomware', 'phishing', 'cyber'],
    subcategories: [
      { slug: 'vulns', name: 'Vulnerabilities', blurb: 'CVEs and disclosed vulnerabilities.', match: ['cve', 'vulnerab', 'zero-day', 'zero day', '0day', 'rce', 'privilege escalation', 'buffer overflow', 'patch tuesday'] },
      { slug: 'research', name: 'Offense & defense', blurb: 'Security research, offensive and defensive.', match: ['exploit', 'reverse engineer', 'fuzzing', 'red team', 'threat actor', 'attack surface', 'side-channel', 'side channel'] },
      { slug: 'supplychain', name: 'Supply chain', blurb: 'Supply-chain and malware.', match: ['supply chain', 'supply-chain', 'malware', 'npm package', 'malicious package', 'backdoor', 'typosquat', 'compromised'] },
      { slug: 'privacy', name: 'Privacy & crypto', blurb: 'Privacy and cryptography.', match: ['privacy', 'encryption', 'cryptograph', 'surveillance', 'tracking', 'anonym', 'end-to-end', 'certificate authority'] },
    ],
  },
  {
    slug: 'hardware',
    name: 'Hardware',
    blurb: 'Silicon, datacenters, and the physical layer.',
    match: ['chip', 'silicon', 'gpu', 'processor', 'semiconductor', 'hardware', 'datacenter', 'data center', 'wafer'],
    subcategories: [
      { slug: 'silicon', name: 'Silicon', blurb: 'Chips and semiconductors.', match: ['chip', ' gpu', ' cpu', 'semiconductor', 'tsmc', 'nvidia', ' arm ', 'risc-v', 'transistor', 'nanometer', 'wafer', 'fab '] },
      { slug: 'datacenter', name: 'Datacenters', blurb: 'Datacenters and power.', match: ['datacenter', 'data center', 'power grid', 'cooling', 'megawatt', 'gigawatt', 'hyperscale', 'interconnect'] },
      { slug: 'devices', name: 'Devices & robotics', blurb: 'Devices, robotics, and the edge.', match: ['robot', 'wearable', 'sensor', 'drone', 'autonomous vehicle', 'edge device', 'humanoid'] },
    ],
  },
  {
    slug: 'industry',
    name: 'Industry',
    blurb: 'Funding, policy, antitrust, and the business of the field.',
    match: ['funding', 'startup', 'acquisition', ' ipo', 'antitrust', 'regulat', 'lawsuit', 'billion', 'revenue', 'layoff'],
    subcategories: [
      { slug: 'funding', name: 'Funding & M&A', blurb: 'Rounds, valuations, and acquisitions.', match: ['funding', 'raises', 'series a', 'series b', 'series c', 'valuation', 'acqui', 'merger', ' ipo', 'venture', 'seed round', 'billion'] },
      { slug: 'policy', name: 'Policy & law', blurb: 'Policy, antitrust, and regulation.', match: ['antitrust', 'regulat', 'lawsuit', ' court', ' ftc', 'export control', 'sanction', 'legislation', ' ban ', 'ruling', 'directive'] },
      { slug: 'labor', name: 'Labor', blurb: 'Labor and the field.', match: ['layoff', 'hiring', 'union', 'workforce', 'remote work', 'job cuts', 'talent'] },
      { slug: 'business', name: 'Company moves', blurb: 'Strategy and company moves.', match: ['partnership', 'earnings', 'revenue', 'expansion', 'shutdown', 'rebrand', 'ceo'] },
    ],
  },
];

/** Legacy pipeline channel slug -> primary category (fallback signal). */
export const CHANNEL_TO_CATEGORY: Record<string, string> = {
  ai: 'ai',
  'ml-research': 'research',
  devtools: 'software',
  security: 'security',
  hardware: 'hardware',
  startups: 'industry',
  science: 'research',
  news: 'industry',
};

/** Tie-break order when two categories score equally (most consequential first). */
const PRIORITY = ['security', 'ai', 'hardware', 'research', 'software', 'industry'];

export const categoryBySlug = (slug: string): Category | undefined =>
  CATEGORIES.find((c) => c.slug === slug);

export function categoryName(slug: string): string {
  return categoryBySlug(slug)?.name ?? slug;
}

/**
 * Derive a single primary category + subcategories from a title and the
 * pipeline's channel tags. Deterministic; mirrors topics.py match_taxonomy.
 */
export function deriveCategory(
  title: string,
  channels: string[] = []
): { category: string; subcategories: string[] } {
  const t = ` ${(title || '').toLowerCase()} `;
  const subHits: Record<string, string[]> = {};
  const score: Record<string, number> = {};

  for (const cat of CATEGORIES) {
    const subs: string[] = [];
    for (const sub of cat.subcategories) {
      if (sub.match.some((m) => t.includes(m))) subs.push(sub.slug);
    }
    let s = subs.length * 2;
    if (cat.match.some((m) => t.includes(m))) s += 1;
    if (subs.length) subHits[cat.slug] = subs;
    if (s) score[cat.slug] = s;
  }
  for (const ch of channels) {
    const c = CHANNEL_TO_CATEGORY[ch];
    if (c) score[c] = (score[c] || 0) + 1;
  }

  let primary = '';
  let best = -1;
  for (const slug of Object.keys(score)) {
    const s = score[slug];
    if (s > best || (s === best && PRIORITY.indexOf(slug) < PRIORITY.indexOf(primary))) {
      best = s;
      primary = slug;
    }
  }
  if (!primary) {
    primary = channels.map((c) => CHANNEL_TO_CATEGORY[c]).find(Boolean) || 'industry';
  }
  const subcategories = (subHits[primary] || []).slice(0, 3);
  return { category: primary, subcategories };
}

/**
 * Resolve a pick's category: trust the pipeline-emitted slug when it names a
 * real category (filtering its subcategories against that category's actual
 * sub-slugs, since an unknown sub would 404 as a link), otherwise fall back
 * to deriveCategory. All pick consumers go through this one helper.
 */
export function resolveCategory(p: {
  title: string;
  channels?: string[];
  category?: string;
  subcategories?: string[];
}): { category: string; subcategories: string[] } {
  const cat = p.category ? categoryBySlug(p.category) : undefined;
  if (cat) {
    const valid = new Set(cat.subcategories.map((s) => s.slug));
    return {
      category: cat.slug,
      subcategories: (p.subcategories ?? []).filter((s) => valid.has(s)).slice(0, 3),
    };
  }
  return deriveCategory(p.title, p.channels ?? []);
}
