"""All LLM prompts for the Telegram Digest application."""

QUESTION_DETECTION_PROMPT = """You are analyzing Telegram messages to identify questions that might benefit from a suggested answer.

A message is a QUESTION if someone is asking for information, help, clarification, or guidance that you (the recipient) might have answered before.

Messages to classify:
{messages}

For each message, respond with JSON:
{{"message_id": <id>, "is_question": true/false, "confidence": 0.0-1.0}}

Only mark as question if:
- It's genuinely seeking information (not rhetorical)
- It's the type of question that gets asked repeatedly
- Someone knowledgeable could provide a useful answer

Do NOT mark as questions:
- Greetings ("How are you?")
- Rhetorical questions
- Questions that are highly context-specific and unlikely to recur
- Questions where the answer changes frequently

Return a JSON array of classifications."""


CHAT_SUMMARY_PROMPT = """Summarize the following conversation from "{chat_name}" for a daily digest.

Messages (chronological order):
{messages}

Write a {detail_level} summary that captures:
- Key topics discussed
- Important decisions or conclusions
- Notable questions raised
- Action items mentioned (if any)

Style guidelines:
- Use present tense ("The team discusses..." not "The team discussed...")
- Be specific about who said what when relevant
- Include concrete details, not vague generalities
- For {detail_level}:
  - "detailed": 200-300 words, comprehensive coverage
  - "standard": 100-150 words, main points
  - "brief": 2-3 bullet points only

Do not include any preamble. Start directly with the summary content."""


DIGEST_AGGREGATE_PROMPT = """Compile these chat summaries into a cohesive daily digest.

Chat summaries (ordered by priority):
{summaries}

Statistics for this period:
- Total messages: {message_count}
- Active chats: {chat_count}
- Time period: {period_start} to {period_end}

Create a digest that:
1. Opens with a 1-2 sentence overview of the day
2. Presents each chat's summary under a clear header
3. Ends with the statistics section

Format for Telegram (use Markdown):
- **Bold** for chat names and emphasis
- `code` for technical terms
- Bullet points for lists

Target length: ~{target_length} words total.
Keep the digest scannable - someone should get the key points in 2 minutes."""


ANSWER_SYNTHESIS_PROMPT = """Synthesize these multiple replies into a single coherent answer.

Original question: "{question}"

Replies to synthesize:
{replies}

Requirements:
- Create one flowing answer that captures all key information
- Preserve attribution: use phrases like "According to Alice..." or "Bob notes that..."
- If replies contradict, present both perspectives with attribution
- Remove redundancy while keeping all unique information
- Maintain the original tone and technical accuracy

Output only the synthesized answer, no preamble."""


ANSWER_ADAPTATION_PROMPT = """You previously answered a similar question with this response:
"{previous_answer}"

The original question was: "{original_question}"

Now someone is asking: "{new_question}"

In chat: {chat_name}

If the previous answer is still applicable:
- Return it, possibly with minor adjustments for context
- Keep the same level of detail and tone

If the previous answer needs significant changes:
- Adapt it while preserving the core information
- Note if important context might be missing

Output only the adapted answer, ready to send."""


# Helper function to format messages for prompts
def format_messages_for_classification(messages: list[dict]) -> str:
    """Format messages for the question detection prompt."""
    lines = []
    for msg in messages:
        lines.append(f"[ID: {msg['id']}] {msg['sender']}: {msg['text']}")
    return "\n".join(lines)


def format_messages_for_summary(messages: list[dict]) -> str:
    """Format messages for the chat summary prompt."""
    lines = []
    for msg in messages:
        timestamp = msg.get('timestamp', '')
        if timestamp:
            timestamp = f" ({timestamp})"
        lines.append(f"{msg['sender']}{timestamp}: {msg['text']}")
    return "\n".join(lines)


def format_replies_for_synthesis(replies: list[dict]) -> str:
    """Format replies for the answer synthesis prompt."""
    lines = []
    for reply in replies:
        lines.append(f"{reply['sender']}: {reply['text']}")
    return "\n\n".join(lines)


def get_detail_level(priority: int) -> str:
    """Get the detail level string based on chat priority."""
    if priority <= 2:
        return "detailed"
    elif priority == 3:
        return "standard"
    else:
        return "brief"
