"""Preload Centrifuge Q&A pairs into the knowledge base."""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from src.config import load_config
from src.store import Store
from src.vectors import VectorStore

# Centrifuge Q&A pairs for preloading
CENTRIFUGE_QA_PAIRS = [
    # What is Centrifuge
    {
        "question": "What is Centrifuge?",
        "answer": "Centrifuge is infrastructure for bringing real-world assets (RWAs) onto blockchain networks. It enables institutional-grade tokenization that bridges traditional finance and decentralized finance (DeFi). The platform allows asset managers to tokenize, distribute, and manage real-world assets like treasuries, credit products, and structured finance on-chain."
    },
    {
        "question": "What does Centrifuge do?",
        "answer": "Centrifuge provides infrastructure for tokenizing real-world assets (RWAs). For asset managers, it offers tools to tokenize assets, distribute them as freely transferable tokens for DeFi, and automate fund administration and reporting. For investors, it provides access to tokenized institutional assets with real-time, verifiable onchain performance data."
    },

    # CFG Token
    {
        "question": "What is the CFG token?",
        "answer": "CFG is the native governance token of Centrifuge. It allows holders to participate in protocol governance through on-chain voting on proposals and runtime upgrades. Voting power is proportional to a holder's CFG stake. As of mid-2025, CFG migrated from Centrifuge Chain to Ethereum as an ERC20 token, consolidating the legacy CFG and WCFG into a single token."
    },
    {
        "question": "What is the CFG token supply?",
        "answer": "The post-migration total supply is 680,000,000 CFG, which includes 115M newly minted tokens for strategic initiatives released gradually. There's a 3% yearly inflation that accrues to the Centrifuge DAO Treasury on Ethereum."
    },
    {
        "question": "How do I migrate my CFG tokens?",
        "answer": "As of May 20, 2025, a new ERC20 CFG went live on Ethereum, replacing both legacy CFG (Centrifuge Chain) and WCFG (Ethereum). Legacy CFG and WCFG holders can swap 1:1 for the new CFG token. Migration ends November 30, 2025."
    },
    {
        "question": "What can I do with CFG tokens?",
        "answer": "CFG tokens are used for: 1) Governance - voting on protocol proposals and upgrades, 2) Chain security on Centrifuge Chain, 3) Earning rewards by investing in pools. Voting power is proportional to your CFG stake."
    },

    # Governance
    {
        "question": "How does Centrifuge governance work?",
        "answer": "Centrifuge uses on-chain governance where CFG token holders vote on proposals. Runtime upgrade proposals are voted on by token holders, and approved proposals are enacted programmatically on-chain. Voting power is proportional to a holder's CFG stake, ensuring decentralized control over the protocol's evolution."
    },
    {
        "question": "What is the Centrifuge Council?",
        "answer": "The Centrifuge Council consists of 9 councilors elected by CFG token holders. The council represents the interests of all Centrifuge stakeholders. Any CFG token holder can submit their candidacy to the council and vote on councilors."
    },

    # Pools and Tranches
    {
        "question": "What are Centrifuge pools?",
        "answer": "Centrifuge pools are fully collateralized asset pools where tokenized real-world assets are pooled together. Issuers can borrow against these pools by offering tokens as collateral. Investors provide liquidity to earn yield. Pools are structured in tranches offering different risk-return profiles."
    },
    {
        "question": "What are DROP and TIN tranches?",
        "answer": "DROP (senior tranche) provides lower but more stable yield with protection from junior tranche holders. TIN (junior tranche) offers higher risk and higher reward - TIN holders accept first risk of loss when borrowers default but can earn higher returns. TIN holders take second priority to DROP holders when earnings are paid out."
    },
    {
        "question": "How do tranches work in Centrifuge?",
        "answer": "Centrifuge pools use a multi-tranche structure. Senior tranches (DROP) offer stable, lower yields with protection. Junior tranches (TIN) offer higher but variable yields and absorb first losses. This allows investors to choose their preferred risk-return profile. Investors lock stablecoins and receive tranche tokens in return."
    },
    {
        "question": "How do I invest in a Centrifuge pool?",
        "answer": "To invest: 1) Research asset originators and pools, 2) Lock DAI/stablecoins into the pool's smart contract, 3) Choose to receive DROP (senior) or TIN (junior) tokens based on your risk preference, 4) Your locked order executes at the end of an epoch at current prices. Pools are 'revolving' so you can invest/redeem flexibly."
    },
    {
        "question": "How do I redeem from a Centrifuge pool?",
        "answer": "To redeem: Lock your DROP/TIN tokens into the smart contract. The order can be cancelled until executed at the end of an epoch at the current price. After execution, you can collect your stablecoins. The decentralized solver mechanism matches redemptions with available liquidity."
    },

    # Liquidity Pools
    {
        "question": "What are Centrifuge Liquidity Pools?",
        "answer": "Liquidity Pools allow users on any supported EVM chain (Ethereum, Base, Arbitrum, Avalanche) to invest in Centrifuge pools of real-world assets. They provide direct integration with any general-purpose EVM blockchain, minimizing effort to integrate new chains. Each tranche is deployed as an ERC-7540 Vault."
    },
    {
        "question": "What chains does Centrifuge support?",
        "answer": "Centrifuge supports multiple chains including Ethereum, Base, Arbitrum, and Avalanche. With the migration to Centrifuge V3 (EVM-native protocol in July 2025), the platform prioritizes EVM compatibility for multichain deployment."
    },

    # Asset Types
    {
        "question": "What types of assets can be tokenized on Centrifuge?",
        "answer": "Centrifuge supports various asset classes including: structured credit, real estate, US treasuries, carbon credits, consumer finance, trade finance receivables, invoices, mortgages, royalties, and corporate debt. These are tokenized as NFTs representing individual assets that can be used as collateral."
    },
    {
        "question": "What are the benefits for asset originators using Centrifuge?",
        "answer": "Benefits include: 1) Bypass traditional banks/intermediaries, 2) Access to more affordable financing (SMEs typically face 15%+ cost of capital vs 1% for large corps), 3) Access new investor pools with fewer barriers, 4) Reduced structural/securitization costs, 5) Self-service daily reporting, 6) Democratized retail investor access."
    },

    # Technical
    {
        "question": "What is Centrifuge Chain?",
        "answer": "Centrifuge Chain is a layer-1 blockchain purpose-built for financing real-world assets, originally built on Substrate/Polkadot. It houses pools, assets, tranches, on-chain governance, treasury, and the CFG token. With Centrifuge V3, the protocol migrated to an EVM-native architecture in July 2025."
    },
    {
        "question": "What is Tinlake?",
        "answer": "Tinlake is Centrifuge's decentralized application (dApp) - a marketplace for tokenized real-world assets. It pools tokenized assets into tranches (DROP/TIN) offering varying risk and return profiles. Investors lock DAI to receive tranche tokens. Tinlake pioneered bringing real-world assets on-chain."
    },

    # Metrics and Partners
    {
        "question": "What is Centrifuge's TVL and scale?",
        "answer": "Centrifuge has over $1.3B+ total value locked with 1,768+ assets tokenized. Over $2B in real-world assets have been tokenized through the platform across 7 blockchain networks. The protocol has financed over $500M in assets."
    },
    {
        "question": "Who are Centrifuge's partners?",
        "answer": "Centrifuge partners include major institutions and DeFi protocols: Janus Henderson, Apollo, Coinbase, S&P Global, MakerDAO (Sky), Aave, Morpho, and other institutional asset managers. The platform integrates with DeFi through standards like ERC-4626 and ERC-7540."
    },

    # Legal and Compliance
    {
        "question": "Can NFTs representing real-world assets be enforced in court?",
        "answer": "Enforcement depends on jurisdiction. NFTs must be tied to RWAs through contractual agreements with borrowers. UK cases indicate legal enforcement is moving in this direction. The key is ensuring proper legal structure connecting the on-chain representation to off-chain assets."
    },

    # Rewards
    {
        "question": "How do I earn CFG rewards?",
        "answer": "You can earn CFG by investing in pools. For Tinlake pools, the rewards rate is approximately 0.0042 CFG per DAI invested per day. You earn rewards from day one but can only claim after a minimum holding period of 30 days."
    },

    # Getting Started
    {
        "question": "How do I get started with Centrifuge?",
        "answer": "To get started: 1) Visit app.centrifuge.io, 2) Connect your wallet, 3) Complete any required onboarding/KYC for institutional pools, 4) Browse available pools and their risk-return profiles, 5) Choose a tranche (senior for stability, junior for higher yield), 6) Deposit stablecoins to invest."
    },
    {
        "question": "Why use blockchain for asset tokenization?",
        "answer": "Blockchain enables: 1) Agreement on shared information without trusted intermediaries, 2) Elimination of numerous intermediaries (managers, lawyers, auditors) that add costs and delays to traditional securitization, 3) Efficiency and reduced errors, 4) Transparency on a digital ledger, 5) 24/7 global accessibility."
    },
]


async def preload_qa():
    """Preload Centrifuge Q&A pairs into the knowledge base."""
    load_config()

    # Initialize components
    store = Store()
    await store.connect()

    vector_store = VectorStore()
    vector_store.connect()

    print(f"Preloading {len(CENTRIFUGE_QA_PAIRS)} Q&A pairs...")

    added = 0
    for qa in CENTRIFUGE_QA_PAIRS:
        # Check if already exists (by question text similarity)
        existing = vector_store.query_similar(qa["question"], threshold=0.95, limit=1)
        if existing:
            print(f"  Skipping (exists): {qa['question'][:50]}...")
            continue

        # Store in SQLite
        qa_id = await store.store_qa_pair(
            question_text=qa["question"],
            answer_text=qa["answer"],
            chat_id=0,
            chat_name="Centrifuge Docs",
            question_message_id=None,
            answer_message_id=None,
            question_from="User",
        )

        # Store in vector DB
        vector_store.add_qa_pair(
            qa_pair_id=qa_id,
            question=qa["question"],
            answer=qa["answer"],
            chat_id=0,
            chat_name="Centrifuge Docs",
        )

        added += 1
        print(f"  Added: {qa['question'][:50]}...")

    print(f"\nDone! Added {added} new Q&A pairs.")
    print(f"Total in vector store: {vector_store.count()}")

    await store.close()


if __name__ == "__main__":
    asyncio.run(preload_qa())
