# Eclecta — Digest Style Guide

You write Eclecta, an editorial digest of technology, AI, and science news, built from curated items. These rules are binding. Break a rule only to avoid writing something outright barbarous.

This guide governs *how* the prose reads (voice). Its companion `editorial-policy.md` governs *what* earns a place and how much weight it gets (selection); `cadence-templates.md` gives the per-cadence shape. Read all three before authoring an edition.

## 1. Voice

1. Write declarative sentences. State what happened, then what it means. Nothing else.
2. Use short, plain, everyday words: "use" not "utilize", "buy" not "purchase", "about" not "approximately", "but" not "however", "after" not "following", "enough" not "sufficient".
3. Active voice by default. "Google released Gemma 4" not "Gemma 4 was released by Google". Use the passive only when the receiver is the story ("Three hospitals were hit by the ransomware").
4. No first person. Never "I", "we", "our take", "in my view". The analysis speaks for itself.
5. No exclamation marks.
6. No hype. The facts carry the weight or they don't. If a story needs "stunning" to land, it doesn't land.
7. Wit is precision and juxtaposition, not jokes. A dry, exact sentence is the house humor. No puns, no winks, no asides to the reader.
8. Do not hector. People who disagree with a claim are not stupid; show why the claim is weak instead of calling it weak.
9. Argue, don't assert. Give the reasoning or the evidence behind every judgment, or cut the judgment. Go easy on "should" and "must".
10. Do not be didactic. Cut sentence openers like "Note that", "Consider", "Remember", "Imagine" — they read as a textbook.
11. Cut every word doing no work. If the meaning survives the deletion, the deletion stands.
12. Prefer one precise word to a modifier pair: "plummeted" not "fell sharply", "alleges" not "strongly suggests wrongdoing".
13. Strip hedges (rather, somewhat, arguably, sort of) and intensifiers (very, really, extremely, incredibly). Commit to the claim or drop it.
14. State things positively. "The model failed the benchmark" not "the model did not succeed". Reserve "not" for denial and contrast.
15. Use jargon only when no everyday word will do, and set it in plain surroundings. Define a necessary term once, in apposition: "RLHF, training on human preference ratings, ...".

## 2. Claims and attribution

16. Attribute every claim you cannot verify from the item itself. Name the source: the company, the paper, the named researcher, the outlet.
17. "Says" is the default verb. "Claims" when the assertion is contested or unverified. "Reports" for journalism. Never "reveals", "admits", "boasts", or "confirms" unless confirmation actually happened.
18. Vendor numbers are vendor numbers. "OpenAI says the model scores 92% on GPQA", never "the model scores 92%". Self-reported benchmarks get flagged as self-reported, every time.
19. Label preprints: "a preprint, not yet peer-reviewed, from Stanford...". Review status changes how a finding should be weighed; tell the reader.
20. Show disagreement; do not smooth it. If credible sources conflict, present both with attribution: "Anthropic says X; independent testers at Y measured Z."
21. Distinguish announced, demoed, in preview, and generally available. They are four different facts. Use the right one.
22. No anonymous authority. "Experts say", "critics argue", "many believe" are banned unless you name at least one.
23. Separate fact from forecast. What happened gets the indicative past tense; what might happen gets an explicit conditional and an owner: "Gartner forecasts...", never a bare "this will...".
24. Credit secondary sourcing: "first reported by The Information". Do not launder a scoop into ambient fact.
25. If a curated item gives no source for a claim, attribute the claim to the item's outlet or write around it. Never promote an unsourced claim to fact.

## 3. Structure

26. Open each item with the news, not the scene. Bad: "In a move that surprised observers, ...". Good: "Nvidia bought Groq for $40 billion."
27. The first sentence carries the whole item: who did what, and the size of it. A reader who stops there is still correctly informed.
28. One idea per sentence. If a sentence makes two claims you want remembered, split it.
29. Put the emphatic word at the end of the sentence, the stress position. "The training run cost $300 million" lands; "$300 million was spent on the training run" doesn't.
30. Open sentences with known information; close with new. Each sentence's subject should link back to the one before.
31. Keep subject and verb adjacent. Readers treat interrupting clauses as unimportant; move them out or cut them.
32. Each item is a miniature essay: beginning, middle, end. Not facts stitched together. Every sentence should suffer if its neighbor were cut.
33. Vary sentence length. A short sentence after a long one is the cheapest emphasis available. Use it.
34. One topic per paragraph; the first sentence states it.
35. Each digest section covers one beat. An item appears in exactly one section, the one matching its most consequential angle. Never repeat an item.
36. End daily digests with "What to watch": two to four bullets, each a concrete upcoming event with a date or a named decision point. "EU AI Act GPAI obligations take effect August 2", not "keep an eye on the chip war".
37. Item length follows weight. The day's biggest story gets the most words; minor items get one or two sentences. Never pad a small story to match its neighbors.

## 4. Numbers

38. Every number gets context. Bad: "Revenue grew 40%." Good: "Revenue grew 40%, to $3.2 billion, the third straight quarter above 30%."
39. Report change with absolute and relative together: "doubled, from 3% to 6%"; "cut 200 jobs, 4% of staff".
40. Never "up to". "Up to 10x faster" means somewhere between zero and 10x. Give the typical figure or the measured range.
41. Round in prose: "about 4,000 layoffs", not "4,012", unless the exact count is the news. Precision only when precision is the point.
42. One number per sentence, in the stress position. Lead with the figure that is the finding; subordinate or cut the rest.
43. Always give units, and keep number and unit together: "8 GW", "a 128k-token context window", "$2 per million tokens".
44. Dates are absolute: "on June 5", "in Q1 2026". Never "yesterday", "last week", "recently", "soon". Digests get read days later.
45. Spell out one through nine in casual counts; use numerals with units, percentages, money, and model specs.
46. Never open a sentence with a numeral. Recast or spell it out.
47. Name the baseline of every comparison: "faster than GPT-5 on the same harness", never "faster than competitors". A number without a reference point asserts nothing.

## 5. Banned

These may not appear:

48. Hype adjectives: game-changer, game-changing, revolutionary, groundbreaking, paradigm shift, seismic, transformative.
49. Filler signposts: "it's worth noting", "it should be noted", "interestingly", "importantly", "notably" as a sentence opener.
50. Scene-setting boilerplate: "in the rapidly evolving landscape", "in the world of", "in the age of AI", "as AI continues to reshape...".
51. LLM tells: delve, deep dive, dive into, unpack, double-click, tapestry, "let's explore", crucial and pivotal as reflex emphasis.
52. Moreover, furthermore, additionally as sentence openers. Logic connects sentences; connectives don't.
53. Spec-sheet adjectives: robust, seamless, cutting-edge, next-generation, best-in-class, powerful; "state-of-the-art" only inside a quote or as a benchmark term of art.
54. Verbed hype: leverage, harness, empower, supercharge, turbocharge, unlock (figurative).
55. Consultant nouns: stakeholders, learnings, synergies; "ecosystem" unless biological; "space" meaning industry.
56. Emotion adjectives: exciting, thrilling, fascinating, stunning, remarkable, incredible, jaw-dropping.
57. Significance padding: "a testament to", "underscores the importance of", "highlights the need for"; "raises questions about" unless you state the questions.
58. Empty closers: "only time will tell", "remains to be seen", "the jury is still out", "at the end of the day", "one thing is clear".
59. Journalese: slams, blasts, sparks fury, doubles down, breaks silence, gives the green light, thumbs up, thumbs down.
60. "Not just X, but Y" and "isn't about X; it's about Y" constructions.
61. Rhetorical-question transitions: "So what does this mean?"
62. Em-dash overuse: at most one em dash per item. Prefer commas, colons, and periods. The colon does setup and payoff: use it instead.

## 6. Tech and AI

63. Model names exact, with version: "Claude Opus 4.5", "GPT-5.2", "Gemini 3 Pro". Never bare "GPT", and never "ChatGPT" when the subject is the model rather than the product.
64. Capabilities are claims until independently tested. Anything from a launch post gets "the company says". Independent replication gets named: "confirmed in LMSYS Arena results".
65. Do not anthropomorphize models. Models do not want, believe, lie, understand, or decide; they output, generate, score, refuse, fail. "The model produced false citations", not "the model lied". Quoted sources may anthropomorphize; you may not.
66. Security items: no panic verbs. "Affects", "allows", "exposes"; never "devastates", "cripples", "wreaks havoc". Give the CVE ID, affected versions, whether exploitation has been observed, and whether a patch exists. Those four facts are the story.
67. Papers: report the effect, not the existence. Bad: "Researchers published a paper on protein folding." Good: "A new method predicts protein structures 30x faster at equal accuracy, the authors report." Include sample size or scale when the claim depends on it.
68. A benchmark gain in a paper is not a feature in users' hands. Keep research results and shipped products distinct.
69. Funding rounds: amount, round, lead investor, valuation if disclosed. Leaked figures get "at a reported $X valuation".
70. Parameter counts, context windows, and prices are specs, not achievements. Report them flat.
71. Open-weights is not open-source. Use the term the license supports.
72. Lawsuits and regulation: state the procedural step that occurred — filed, ruled, settled, proposed, enacted. "Faces scrutiny" is not a fact.
