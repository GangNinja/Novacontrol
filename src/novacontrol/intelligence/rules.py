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
        # ── Screen/image ANALYSIS (distinct from capturing a screenshot) ─
        # Placed before the reasoning rules: "look at this screenshot" is a
        # vision request, not a question about the word "screenshot".
        IntentRule(
            IntentName.SCREENSHOT_ANALYSIS,
            prefixes=("look at this screenshot", "look at the screenshot", "look at this image",
                      "read this image", "read the error in this screenshot", "analyse this image",
                      "analyze this image", "analyse this screenshot", "analyze this screenshot",
                      "describe what you see", "what does this image show", "what is in this image"),
            patterns=("what is on my screen", "what's on my screen", "what is on the screen",
                      "what's on the screen", "read my screen", "look at my screen",
                      "read the error on screen", "describe my screen", "what do you see on screen"),
            regex=(r"\b(?:screenshot|image|picture|screen)\b.*\b(?:tell me what|what is wrong|what's wrong|explain|analyse|analyze|describe|read)\b",),
            confidence=0.85,
        ),
        # ── System status: deterministic answers, never a language model ──
        # These sit ABOVE the research/answer rules on purpose. "check memory
        # usage" used to match the research prefix "check " and route a RAM
        # question into the web pipeline; the specific reading wins.
        IntentRule(
            IntentName.MEMORY_STATUS,
            prefixes=("check memory", "check my memory", "show memory", "show my memory", "show memory usage",
                      "show ram", "show my ram", "check ram", "check my ram", "memory usage", "ram usage"),
            patterns=("how much memory", "how much ram", "memory usage", "ram usage", "free memory",
                      "available memory", "what is using my ram", "what's using my ram",
                      "what is consuming my ram", "what's consuming my ram", "using my ram",
                      "using my memory", "eat my memory", "eating my memory", "memory almost full",
                      "out of memory", "most ram", "most memory"),
            confidence=0.92,
        ),
        IntentRule(
            IntentName.CPU_STATUS,
            prefixes=("check cpu", "check my cpu", "show cpu", "cpu usage", "cpu load", "cpu status"),
            patterns=("cpu usage", "cpu load", "my processor", "processor usage", "using my cpu",
                      "cpu maxed out", "cores am i using", "how busy is my processor", "cpu temperature"),
            confidence=0.92,
        ),
        IntentRule(
            IntentName.GPU_STATUS,
            prefixes=("check gpu", "check my gpu", "show gpu", "gpu usage", "gpu status", "vram"),
            patterns=("gpu usage", "gpu doing", "graphics card", "video card", "gpu temperature",
                      "is my gpu", "vram usage"),
            confidence=0.9,
        ),
        IntentRule(
            IntentName.BATTERY_STATUS,
            prefixes=("check battery", "battery status", "battery level", "battery percentage"),
            patterns=("battery", "on battery", "charging", "until my laptop dies", "plugged in"),
            regex=(r"\bbattery\b.*\b(?:left|percentage|level|full|low|charging|status)\b",),
            confidence=0.9,
        ),
        IntentRule(
            IntentName.NETWORK_STATUS,
            prefixes=("check network", "check my network", "network status", "check my connection",
                      "check the connection", "wifi status", "check my internet", "check the internet"),
            patterns=("am i online", "is my internet", "internet working", "network issues", "network speed",
                      "connected to wifi", "wifi connected", "network connection", "internet connection",
                      "internet down", "loosing connection", "losing connection", "packet loss", "network usage"),
            confidence=0.9,
        ),
        IntentRule(
            IntentName.SYSTEM_STATUS,
            prefixes=("system status", "system health", "check system health", "system report", "uptime"),
            patterns=("how is my computer", "how is my system", "is my computer healthy", "is my system healthy",
                      "system doing", "give me a status report", "what is my uptime", "my computer specs",
                      "temperature of my computer"),
            confidence=0.88,
        ),
        # ── Device controls ────────────────────────────────────────────────
        IntentRule(
            IntentName.VOLUME_CONTROL,
            prefixes=("set volume", "set the volume", "volume to", "turn the volume", "turn volume",
                      "change the volume", "change volume", "mute", "unmute", "louder", "quieter", "raise volume", "lower volume"),
            patterns=("volume up", "volume down", "sound up", "sound down", "turn it up", "turn it down"),
            entity="level",
            confidence=0.9,
        ),
        IntentRule(
            IntentName.BRIGHTNESS_CONTROL,
            prefixes=("set brightness", "set the brightness", "brightness to", "change the brightness",
                      "change brightness", "increase brightness", "decrease brightness", "dim the screen",
                      "brighten the screen"),
            patterns=("screen brightness", "display brightness", "make the screen brighter", "screen too bright", "screen too dark"),
            entity="level",
            confidence=0.9,
        ),
        IntentRule(
            IntentName.MEDIA_CONTROL,
            prefixes=("pause the", "pause music", "play the", "resume playback", "stop the music",
                      "stop the video", "next song", "previous song", "skip this"),
            patterns=("next track", "previous track", "skip track", "pause music", "play music", "media control", "stop playback"),
            confidence=0.85,
        ),
        # ── Arithmetic (narrow: "what is 2+2" stays a general question) ─────
        IntentRule(
            IntentName.CALCULATE,
            prefixes=("calculate ", "compute ", "what is the total of", "percent of", "% of "),
            regex=(r"^\d+\s*[-+*/x×]\s*\d+",),
            entity="expression",
            confidence=0.85,
        ),
        # ── Code and project work ─────────────────────────────────────────
        IntentRule(
            IntentName.CODE_DEBUGGING,
            prefixes=("debug ", "fix this error", "fix the error", "fix my code", "fix this bug",
                      "find the bug", "why is my test failing", "why are my tests failing",
                      "traceback", "stack trace", "this is broken", "my code does not work", "my code doesn't work"),
            regex=(r"\b(?:error|bug|crash|traceback|exception)\b.*\b(?:my|this|the)\s+(?:code|script|function|app|project)\b",),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.CODE_EXPLANATION,
            regex=(r"\b(?:explain|what does|why is|walk me through|how does)\b[^.]*\b(?:code|function|script|class|snippet|algorithm|regex|query)\b",),
            patterns=("explain this function", "explain this code", "explain the code", "what does this code do"),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.CODE_GENERATION,
            prefixes=("write a function", "write a script", "write a class", "write a program",
                      "write a python", "write some code", "generate a function", "generate a script",
                      "generate a class", "create a function", "create a script", "create a class",
                      "implement a ", "implement an ", "refactor "),
            patterns=("write code that", "write me a function", "write me a script"),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.PROJECT_ANALYSIS,
            prefixes=("analyse my project", "analyze my project", "analyse the project", "analyze the project",
                      "review the codebase", "review my codebase", "review the project", "analyse the codebase",
                      "analyze the codebase", "explain the project structure",
                      "overview of the project", "overview of my project"),
            patterns=("project structure", "codebase structure", "what does this repository do",
                      "what does this project do", "walk me through the codebase"),
            confidence=0.85,
        ),
        # ── Files: read / find / modify / delete / move / copy ─────────────
        # Content files have no launcher, so "read notes.txt" and "open
        # notes.txt" both mean READ the file. This rule therefore has to be
        # registered BEFORE open_application, which would otherwise claim
        # "open notes.txt" as an application literally called "notes.txt".
        IntentRule(
            IntentName.READ_FILE,
            prefixes=("read my ", "read the contents of", "show the contents of",
                      "show me the contents of", "what's in ", "whats in "),
            regex=(
                r"^(?:read|show|display|view|cat|print)\b[^.]*\.[a-z0-9]{2,5}\b",
                r"^(?:open|launch|start)\b[^.]*\.(?:txt|md|csv|tsv|log|json|ya?ml|toml|ini|"
                r"cfg|conf|xml|py|js|ts|tsx|jsx|html|css|sh|bash|bat|ps1|sql|env)\b",
            ),
            entity="file",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.FIND_FILE,
            prefixes=("find file", "find the file", "find my file", "find a file", "locate the file",
                      "locate my file", "locate a file", "where is my ", "where's my ", "where is the file"),
            regex=(r"\b(?:find|locate|search for)\b[^.]*\.[a-z0-9]{2,5}\b",),
            entity="file",
            confidence=0.85,
        ),
        # A project is named without an extension ("find my NovaControl
        # project"), so it needs its own rule rather than the filename regex.
        IntentRule(
            IntentName.FIND_FILE,
            prefixes=("find my project", "find the project", "locate my project",
                      "locate the project", "where is my project", "where's my project"),
            regex=(r"^(?:find|locate|where\s+is)\s+(?:my|our|the)?\s*[\w][\w .&+-]{1,40}?\s+project\b",),
            entity="project",
            confidence=0.85,
        ),
        # A folder named in prose ("open my report folder") names a target, so
        # it gets a rule instead of being resolved as a bare "the folder"
        # reference — and being claimed by open_application, whose "open "
        # prefix would otherwise read the whole phrase as an app name.
        IntentRule(
            IntentName.OPEN_FOLDER,
            prefixes=("open my folder", "show my folder"),
            regex=(r"^(?:open|show|go\s+to)\s+(?:my|our|the)?\s*[\w][\w .-]{1,40}?\s+(?:folder|directory)\b",),
            entity="folder",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.MODIFY_FILE,
            prefixes=("edit the file", "edit my file", "change the file", "change my file", "update the file",
                      "update my file", "append to ", "add a line to ", "modify the file", "modify my file"),
            patterns=("add a line to", "change the value in", "edit the contents of", "update the readme"),
            entity="file",
            confidence=0.82,
        ),
        IntentRule(
            IntentName.DELETE_FILE,
            prefixes=("delete file", "delete the file", "delete my file", "remove file", "remove the file",
                      "trash the file", "get rid of the file"),
            patterns=("delete the file", "delete this file", "remove the file", "delete my file"),
            regex=(r"\b(?:delete|remove|trash|get rid of)\b[^.]*\.[a-z0-9]{2,5}\b",),
            entity="file",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.MOVE_FILE,
            prefixes=("move the file", "move my file", "relocate the file", "relocate my file"),
            regex=(r"\bmove\b[^.]*\.[a-z0-9]{2,5}\b[^.]*\b(?:to|into)\b",),
            patterns=("move the file", "move my file"),
            entity="file",
            confidence=0.85,
        ),
        IntentRule(
            IntentName.COPY_FILE,
            prefixes=("copy the file", "copy my file", "duplicate the file", "duplicate my file", "make a copy of "),
            patterns=("make a copy of", "copy the file", "copy my file", "duplicate the file"),
            regex=(r"\bcopy\b[^.]*\.[a-z0-9]{2,5}\b",),
            entity="file",
            confidence=0.85,
        ),
        # ── Conversation (kept last among the new rules so action phrasing wins) ─
        IntentRule(
            IntentName.CONVERSATION,
            exact=("hi", "hello", "hey", "yo", "hiya", "good morning", "good evening", "good afternoon",
                   "thanks", "thank you", "thanks!", "cheers", "bye", "goodbye"),
            prefixes=("how are you", "who are you", "what are you", "good to see you"),
            confidence=0.85,
        ),
        IntentRule(
            IntentName.GENERAL_QUESTION,
            prefixes=("quick question", "i have a question", "can i ask you", "may i ask"),
            entity="question",
            confidence=0.8,
        ),
        IntentRule(
            IntentName.OPEN_APPLICATION,
            prefixes=("open ", "launch ", "start ", "run app", "fire up", "bring up"),
            regex=(r"^(?:can you |could you |please )?(?:open|launch|start|bring up)\b",),
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
