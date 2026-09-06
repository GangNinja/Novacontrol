"""Default global intent rules — ONE table derived from NovaControl's
capabilities. The brain's keyword classifier and the device parsers consult
this layer instead of maintaining private phrase lists.
"""

from __future__ import annotations

from novacontrol.intelligence.intent import IntentName, IntentRule


def default_rules() -> tuple[IntentRule, ...]:
    return (
        # ── Device control (phone) ──────────────────────────────────────
        IntentRule(
            IntentName.PHONE_CONNECT,
            prefixes=("connect phone", "connect my phone", "pair phone", "pair my phone",
                      "connect the phone", "pair a phone", "phone connect"),
            exact=("connect", "pair"),
            confidence=0.9,
        ),
        IntentRule(
            IntentName.PHONE_STATUS,
            patterns=("bridge status", "phone status", "phone bridge", "pairing status"),
            confidence=0.9,
        ),
        IntentRule(
            IntentName.PHONE_SEND_TEXT,
            prefixes=("text ", "sms ", "send a text", "send text", "send an sms", "send sms", "message "),
            patterns=("send a text", "send text", "send an sms"),
            entity="message",
            confidence=0.88,
        ),
        IntentRule(
            IntentName.PHONE_CALL,
            prefixes=("call ", "dial ", "phone "),
            entity="contact",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.PHONE_SCREENSHOT,
            regex=(r"\bscreenshot\b.*\b(?:phone|android|mobile)\b", r"\b(?:phone|android|mobile)\b.*\bscreenshot\b"),
            confidence=0.9,
        ),
        IntentRule(
            IntentName.PHONE_OPEN_APP,
            regex=(r"\b(?:whatsapp|youtube|gmail)\b.*\b(?:phone|android|mobile)\b",),
            entity="application",
            confidence=0.85,
        ),
        # ── Desktop ─────────────────────────────────────────────────────
        IntentRule(
            IntentName.TAKE_SCREENSHOT,
            prefixes=("take a screenshot", "take screenshot", "screenshot my screen", "screenshot the screen", "capture my screen", "capture the screen", "capture screen", "screen capture"),
            exact=("screenshot",),
            confidence=0.92,
        ),
        IntentRule(
            IntentName.OPEN_APPLICATION,
            prefixes=("open ", "launch ", "start ", "run app", "fire up"),
            regex=(r"^(?:can you |could you |please )?(?:open|launch|start)\b",),
            entity="application",
            confidence=0.9,
        ),
        IntentRule(
            IntentName.CLOSE_APPLICATION,
            prefixes=("close ", "quit ", "exit ", "kill "),
            entity="application",
            confidence=0.8,
        ),
        IntentRule(
            IntentName.OPEN_FOLDER,
            prefixes=("open folder", "open the folder", "open directory", "show folder", "go to folder", "open file explorer"),
            entity="folder",
            confidence=0.9,
        ),
        IntentRule(
            IntentName.TYPE_TEXT,
            prefixes=("type ", "write ", "enter text", "input text"),
            entity="text",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.PRESS_KEY,
            prefixes=("press ", "hit the key", "press the key"),
            entity="key",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.RUN_COMMAND,
            prefixes=("run script", "execute script", "run command", "execute command", "run in terminal", "terminal command"),
            entity="command",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.ORGANIZE_FILES,
            patterns=("organize files", "organize my files", "tidy my files", "sort my files"),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.LIST_FILES,
            prefixes=("list files", "list the files", "show files", "list directory", "show directory"),
            entity="directory",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.READ_FILE,
            prefixes=("read file", "read the file", "open file ", "show file contents"),
            entity="file",
            confidence=0.8,
        ),
        IntentRule(
            IntentName.WRITE_FILE,
            prefixes=("write file", "create file", "save file", "write to file"),
            entity="file",
            confidence=0.8,
        ),
        IntentRule(
            IntentName.SYSTEM_INFO,
            patterns=("system info", "system information", "computer info", "my system specs"),
            confidence=0.85,
        ),
        # ── Browser ─────────────────────────────────────────────────────
        IntentRule(
            IntentName.SEARCH_WEB,
            patterns=("search the web", "search the internet", "web search", "internet search", "search online"),
            prefixes=("google ", "search web", "look up on the web", "search for"),
            regex=(r"^search\b(?! the web\b)",),
            entity="query",
            confidence=0.86,
        ),
        IntentRule(
            IntentName.NAVIGATE,
            prefixes=("navigate to", "go to", "visit ", "browse to", "take me to", "open website", "open the website", "open site", "open the site"),
            entity="url",
            confidence=0.9,
        ),
        IntentRule(
            IntentName.FILL_FORM,
            prefixes=("fill the form", "fill form", "fill out the form", "fill out form", "complete the form", "submit the form"),
            entity="form",
            confidence=0.88,
        ),
        IntentRule(
            IntentName.EXTRACT_PAGE,
            prefixes=("extract from", "scrape ", "get page contents", "read the page", "read page"),
            entity="selector",
            confidence=0.8,
        ),
        # ── Research / reasoning ────────────────────────────────────────
        IntentRule(
            IntentName.CHECK_STATUS,
            prefixes=("check status", "system health", "status report", "what is your status"),
            patterns=("system status", "health check"),
            regex=(r"^check\b.*\b(status|health)\b", r"^how (?:are|is) (?:you|the system|novacontrol)\b"),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.RESEARCH,
            prefixes=("research ", "look into ", "find out about", "dig into", "check ", "check on ", "look up "),
            patterns=("do research", "research on"),
            entity="topic",
            confidence=0.88,
        ),
        IntentRule(
            IntentName.COMPARE,
            prefixes=("compare ", "difference between", "versus ", " vs "),
            entity="topic",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.SUMMARIZE,
            prefixes=("summarize ", "summarise ", "tldr", "tl;dr", "give me a summary"),
            entity="topic",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.ANSWER_QUESTION,
            prefixes=("what is", "what are", "who is", "who was", "why is", "why are", "how does", "how do", "explain", "tell me about", "tell me what", "tell me how", "tell me why", "tell me when", "tell me if"),
            entity="topic",
            confidence=0.8,
        ),
        IntentRule(
            IntentName.GENERATE_REPORT,
            patterns=("generate a report", "generate report", "create a report", "write a report", "save the report", "save a report"),
            prefixes=("save report", "write report", "save the report"),
            entity="topic",
            confidence=0.85,
        ),
        # ── Orchestration / projects / memory ───────────────────────────
        IntentRule(
            IntentName.CREATE_AUTOMATION,
            patterns=("create automation", "create an automation", "new automation", "automate "),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.RUN_AUTOMATION,
            patterns=("run automation", "run the automation", "execute workflow", "run workflow"),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.SCHEDULE_TASK,
            patterns=("tomorrow", "later today", "next week", "remind me", "schedule "),
            confidence=0.7,
        ),
        IntentRule(
            IntentName.REMEMBER,
            prefixes=("remember ", "remember that", "keep in mind", "note that"),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.RECALL,
            prefixes=("recall", "what did i", "when did i", "what have we"),
            confidence=0.8,
        ),
        IntentRule(
            IntentName.PLAN_TASK,
            prefixes=("plan ", "make a plan", "create a plan", "break down ", "roadmap "),
            confidence=0.82,
        ),
        IntentRule(
            IntentName.AGENTIC_TASK,
            patterns=("open github", "check github", "go to github", "my github"),
            confidence=0.8,
        ),
        IntentRule(
            IntentName.CREATE_PROJECT,
            prefixes=("create project", "new project", "start a project"),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.IMPROVE_SELF,
            patterns=("improve yourself", "improve novacontrol", "self improvement", "optimize yourself"),
            confidence=0.85,
        ),
    )
