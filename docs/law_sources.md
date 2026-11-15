# Law sources for `prova/law.py` (fetched 2026-11-15, nothing from memory)

Status legend: **P** primary/official · **A** archive copy of the official EU text (legislation.gov.uk,
frozen at 31 Dec 2020) · **S** secondary (law firm, commentary, private law database) · **X** not retrieved.

Rule for WP0: a law item may be encoded in `law.py` only with a source id below; its quote must be found in
the stored snapshot (`law/sources/<id>.*`), checked by a test, like slot quotes. Items marked X or S-only are
shown in the Compliance Brief as **"unverified, confirm with counsel"** and are never used to allow anything.

EUR-Lex returned HTTP 202 with an empty body for every automated request (2002/58, 2016/97, 2016/679,
2024/1689, 2023/2673, 2026/1744). **Reviewer action:** download these from EUR-Lex in a browser as PDF/HTML
into `law/sources/`: CELEX 02002L0058-20091219, 02016L0097-20200612, 32016R0679, 32024R1689, 32023L2673,
32026R1744.

| id | Law / article | Status | URL | What the fetched text says (short quotes / close paraphrase) |
|---|---|---|---|---|
| `eu-eprivacy-13` | Dir. 2002/58/EC art. 13 (as substituted by 2009/136/EC) | A | https://www.legislation.gov.uk/eudr/2002/58/article/13 | §1 automated calling systems, fax, e-mail for direct marketing "may be allowed only in respect of subscribers or users who have given their prior consent". §2 own customers' electronic contact details from a sale: "may use these electronic contact details for direct marketing of its own similar products or services", with free easy objection at collection and in each message. §4 no concealed sender, valid opt-out address required |
| `eu-eprivacy-2h` | Dir. 2002/58/EC art. 2(h) "electronic mail" | A | https://www.legislation.gov.uk/eudr/2002/58/article/2 | "any text, voice, sound or image message sent over a public communications network which can be stored…". SMS not named; inclusion is an inference |
| `gr-3471-11` | Ν. 3471/2006 art. 11 "Μη ζητηθείσα επικοινωνία" | S (original 2006 text, **not consolidated**) | https://www.dsanet.gr/Epikairothta/Nomothesia/n3471_06.htm | §1 automated calling systems and generally any unsolicited communication by any electronic means, **with or without human intervention**, for direct marketing: "επιτρέπεται μόνο αν ο συνδρομητής συγκατατεθεί εκ των προτέρων ρητώς". §2 not allowed where the subscriber told the provider it does not want them; provider keeps a free special list. §3 e-mail contact details from a sale may promote similar products of the same supplier without prior consent, with easy free objection in each message. §4 sender identity and valid opt-out address required. §5 also legal persons |
| `gr-3471-11-amend` | amendments of art. 11 (ν. 3917/2011, ν. 4070/2012) | X | dpa.gr (HTTP 403), forin.gr (HTTP 403) | consolidated text not retrieved. Secondary sources (DPA Directive 2/2011 summary, consumer sites) say §1 = automated calls need consent, §2 = opt-out "Μητρώο 11" for human calls, §3 covers e-mail and SMS under conditions: **unverified** |
| `eu-ai-act-50` | Reg. 2024/1689 art. 50(1), (5) | S (text) + P (Commission FAQ/guidelines) | https://artificialintelligenceact.eu/article/50/ · https://digital-strategy.ec.europa.eu/en/faqs/transparency-obligations-under-article-50-ai-act · https://digital-strategy.ec.europa.eu/en/library/guidelines-transparency-obligations-providers-and-deployers-ai-systems | 50(1) systems "intended to interact directly with natural persons" must ensure people "are informed that they are interacting with an AI system", unless obvious. 50(5) "in a clear and distinguishable manner", "at the latest at the time of the first interaction". Commission FAQ: "People must be notified when they are interacting with an AI system from the start of the first interaction"; covers "chatbots, AI agents, and avatars"; obvious-exception read "in a restrictive manner". Guidelines page: "These transparency obligations apply from 2 August 2026." No voice/phone-specific guidance found on those pages (guidelines PDF not fetched) |
| `eu-ai-omnibus` | Reg. 2026/1744 (Digital Omnibus) | S | https://www.morganlewis.com/blogs/sourcingatmorganlewis/2026/08/eu-ai-acts-transparency-rules-what-went-into-effect-on-2-august · https://www.joneswalker.com/en/insights/blogs/ai-law-blog/yes-august-2-still-matters-the-eu-approved-a-high-risk-ai-delay-but-most-trans.html | "On 2 August 2026, the transparency obligations in Article 50 … began to apply." Transition to 2 Dec 2026 only for provider-side machine-readable marking (50(2)). OJ text not retrieved |
| `eu-idd-20` | Dir. 2016/97 art. 20 | A | https://www.legislation.gov.uk/eudr/2016/97/article/20 | §1 before the contract the distributor "shall specify, on the basis of information obtained from the customer, the demands and the needs of that customer"; contract proposed consistent with them. §4 product information "prior to the conclusion of a contract, whether or not advice is given". §5 non-life: standardised insurance product information document (IPID) "on paper or on another durable medium" |
| `eu-idd-23` | Dir. 2016/97 art. 23 | A | https://www.legislation.gov.uk/eudr/2016/97/article/23 | §1 information on paper, clear, official language, free; §2–6 durable medium / website under conditions, paper on request. §7 telephone selling: pre-contract information incl. IPID given under the EU distance-marketing rules for financial services, and on paper/durable medium **immediately after** the contract is concluded |
| `gr-4583-33-7` | Ν. 4583/2018 art. 33 §7 | S | https://www.taxheaven.gr/law/4583/2018/arthro/33 | "Στην περίπτωση πώλησης μέσω τηλεφώνου": pre-contract info incl. the product information document given under the distance-marketing law; in every case given in writing (§1/§2) immediately after conclusion. §1 information "εγγράφως και δωρεάν, στην ελληνική γλώσσα" (or another agreed language) |
| `gr-4583-28-29` | Ν. 4583/2018 art. 28, 29 | S | https://www.taxheaven.gr/law/4583/2018/arthro/28 · /arthro/29 | pre-contract disclosures by intermediary/insurer "εγκαίρως και πριν από τη σύναψη" (identity, register, advice basis, remuneration). The **demands-and-needs article of the Greek law was not located** (not in 28/29) |
| `eu-dmfs-2023-2673` | Dir. 2023/2673 (distance financial services, repeals 2002/65) | S (dates only) / X (text) | search results only (law-firm notes) | repeal of 2002/65 and application of new rules from 19 June 2026 per secondary sources. Withdrawal period/conditions **not retrieved**; Greek transposition **not found** |
| `eu-gdpr-21` | GDPR art. 21(2)–(4) | S | https://gdpr-info.eu/art-21-gdpr/ | direct marketing: "the data subject shall have the right to object at any time"; after objection "the personal data shall no longer be processed for such purposes"; the right must be "explicitly brought to the attention of the data subject" at the latest at the first communication |
| `eu-gdpr-13` | GDPR art. 13(1)(a)–(c) | S | https://gdpr-info.eu/art-13-gdpr/ | at collection: controller identity and contact, DPO contact where applicable, purposes and legal basis |
| `eu-gdpr-22` | GDPR art. 22(1) | S | https://gdpr-info.eu/art-22-gdpr/ | right "not to be subject to a decision based solely on automated processing" with legal or similarly significant effects |

## What changes in the design because of these sources

1. **Human-call lane is OFF by default for GR.** The fetched (original) art. 11 §1 covers unsolicited
   marketing "with or without human intervention". The consolidated text was not retrieved. Until the
   reviewer stores the consolidated text, the human lane stays disabled for GR; the owner can enable it only
   with that source present.
2. **New floor item: right to object at the first contact** (GDPR 21(4)). The opening / consent page must
   say the customer can object at any time and how. Added as `must_say` stage `opening` in the floor pack.
3. **Consent-request channel.** The fetched GR §3 names e-mail for the existing-customer exception; SMS is
   not in the fetched text. With the wifi-only demo there is no SMS anyway: requests go by web link / QR,
   and in production by e-mail (§3 conditions: same supplier, similar products, opt-out in every message).
4. **Insurance after-call duty:** IDD 23(7) / GR 4583 33§7: information on paper or durable medium
   immediately after a telephone sale. The confirmation page must carry the product information document
   and be downloadable (durable medium). Added to the insurance pack proof fields.
5. **Withdrawal period:** the 14 days in `oroi.md` is the company's own statement. The law text
   (2023/2673 and its Greek transposition) was not retrieved, so the Brief cites the company document, not a law.
6. **AI disclosure timing confirmed by the Commission:** from the start of the first interaction, clearly.
   Our fixed opening already does this. Applies since 2 Aug 2026.
