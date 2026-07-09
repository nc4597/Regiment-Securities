def summarize_scan_text(scan_text: str) -> str:
    return (
        "You are a compliance review assistant. "
        "Read the brokerage review output below and produce a concise executive summary. "
        "Do not add any facts not present in the text. "
        "Use plain language, mention whether current-month issues were found, and note any prior-vs-current comparison observations. "
        "Return the result as a short paragraph followed by up to three bullet points.\n\n"
        "Review data:\n\n"
        f"{scan_text.strip()}"
    )


def prompt_for_review(summary_text: str) -> str:
    return (
        "You are a financial compliance analyst. "
        "Analyze this brokerage review summary and highlight the most important compliance findings, "
        "any risks that need follow-up, and whether there are significant prior-month differences. "
        "Respond in short, actionable language.\n\n"
        "Brokerage review summary:\n\n"
        f"{summary_text.strip()}"
    )
