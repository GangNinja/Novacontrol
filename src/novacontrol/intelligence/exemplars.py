"""Intent exemplar corpus: how people actually phrase each intent.

This is the lexical layer's *knowledge*, kept separate from the rule registry's
*precision*. The split is deliberate:

  * ``rules.py`` matches exactly (prefixes, needles, regex) — predictable, and
    the only thing allowed to be fully trusted;
  * this corpus teaches imprecise similarity — paraphrases, synonyms, and the
    casual phrasings that a prefix table can never enumerate.

Two consequences worth stating, because they are the reason this is a data file
and not logic:

  * adding a way of speaking is a one-line data change, no engine edit;
  * "memory" vs "ram" is not something TF-IDF can discover on its own — the
    synonym has to be demonstrated, so both phrasings appear below.

Exemplars must be phrasings a USER would say, never the internal name of the
intent. A corpus full of identifiers ("memory_status") would make the matcher
score jargon highly and real language poorly.
"""

from __future__ import annotations

from novacontrol.intelligence.intent import IntentName

# Per-intent example phrasings. Kept to a handful each on purpose: with a
# corpus this small the index is exact, and a long list of near-duplicates
# would dilute the IDF weights that make distinctive words (ram, battery,
# brightness) decisive.
_EXEMPLARS: tuple[tuple[IntentName, tuple[str, ...]], ...] = (
    # ── Applications ────────────────────────────────────────────────────────
    (
        IntentName.OPEN_APPLICATION,
        (
            "open chrome",
            "launch chrome",
            "start chrome",
            "bring up chrome",
            "can you open notepad",
            "please open the calculator",
            "fire up spotify",
            "open vs code",
            "open the terminal app",
            "i want to use the paint program",
        ),
    ),
    (
        IntentName.CLOSE_APPLICATION,
        (
            "close chrome",
            "quit notepad",
            "exit steam",
            "kill the browser",
            "shut down spotify",
            "close the calculator window",
        ),
    ),
    (
        IntentName.OPEN_FOLDER,
        (
            "open my downloads folder",
            "show me the documents folder",
            "go to the pictures directory",
            "open file explorer in projects",
        ),
    ),
    # ── Files ───────────────────────────────────────────────────────────────
    (
        IntentName.FIND_FILE,
        (
            "find report.pdf",
            "where is my tax return file",
            "locate the invoice document",
            "search my computer for a file called notes",
            "find any spreadsheets i have",
        ),
    ),
    (
        IntentName.LIST_FILES,
        (
            "list the files in this folder",
            "show me what is in the downloads directory",
            "what files are in my project",
        ),
    ),
    (
        IntentName.READ_FILE,
        (
            "read report.pdf",
            "show me the contents of notes.txt",
            "open the config file and tell me what it says",
            "what does readme say",
        ),
    ),
    (
        IntentName.WRITE_FILE,
        (
            "create a file called todo.txt",
            "write a new file with my notes",
            "save this to a file",
            "make a text file for the shopping list",
        ),
    ),
    (
        IntentName.MODIFY_FILE,
        (
            "add a line to notes.txt",
            "change the value in the config file",
            "update the readme",
            "append this to my todo list file",
            "edit the settings file",
        ),
    ),
    (
        IntentName.DELETE_FILE,
        (
            "delete old_report.pdf",
            "remove the temp file",
            "get rid of that draft document",
            "trash the backup file",
        ),
    ),
    (
        IntentName.MOVE_FILE,
        (
            "move report.pdf to the archive folder",
            "put notes.txt in documents",
            "relocate the invoice to the finance directory",
        ),
    ),
    (
        IntentName.COPY_FILE,
        (
            "copy report.pdf to the backup folder",
            "duplicate this file",
            "make a copy of notes.txt on the desktop",
        ),
    ),
    (
        IntentName.ORGANIZE_FILES,
        (
            "organize my files",
            "tidy up the downloads folder",
            "sort my files by type",
            "clean up my desktop clutter",
        ),
    ),
    # ── System status: the deterministic surface ────────────────────────────
    (
        IntentName.MEMORY_STATUS,
        (
            "show my ram",
            "how much memory am i using",
            "check memory usage",
            "what is consuming my ram",
            "how much ram do i have",
            "which programs are eating my memory",
            "is my memory almost full",
            "show applications using the most ram",
            "what is using all my memory",
        ),
    ),
    (
        IntentName.CPU_STATUS,
        (
            "what is my cpu usage",
            "how busy is my processor",
            "check cpu load",
            "what is using my cpu",
            "how many cores am i using",
            "is my cpu maxed out",
        ),
    ),
    (
        IntentName.GPU_STATUS,
        (
            "what is my gpu doing",
            "show gpu usage",
            "is my graphics card being used",
            "check the gpu temperature",
        ),
    ),
    (
        IntentName.BATTERY_STATUS,
        (
            "is my battery full",
            "how much battery do i have left",
            "what is my battery percentage",
            "am i on battery power",
            "how long until my laptop dies",
        ),
    ),
    (
        IntentName.NETWORK_STATUS,
        (
            "am i online",
            "check my network connection",
            "is my internet working",
            "any network issues",
            "what is my network speed",
            "am i connected to wifi",
        ),
    ),
    (
        IntentName.SYSTEM_STATUS,
        (
            "how is my system doing",
            "give me a system status report",
            "show my computer specs",
            "is my computer healthy",
            "check system health",
            "what is my uptime",
        ),
    ),
    # ── Device controls ─────────────────────────────────────────────────────
    (
        IntentName.VOLUME_CONTROL,
        (
            "set volume to 40%",
            "turn the volume down",
            "make it louder",
            "mute the sound",
            "turn the volume up to maximum",
        ),
    ),
    (
        IntentName.BRIGHTNESS_CONTROL,
        (
            "set brightness to 50%",
            "dim the screen",
            "make the screen brighter",
            "lower the display brightness",
        ),
    ),
    (
        IntentName.MEDIA_CONTROL,
        (
            "pause the music",
            "play the next song",
            "skip this track",
            "stop the video",
            "resume playback",
        ),
    ),
    # ── Screens and vision ──────────────────────────────────────────────────
    (
        IntentName.TAKE_SCREENSHOT,
        (
            "take a screenshot",
            "capture my screen",
            "grab a screenshot of this window",
            "screenshot my phone",
        ),
    ),
    (
        IntentName.SCREENSHOT_ANALYSIS,
        (
            "look at this screenshot and tell me what is wrong",
            "what is on my screen right now",
            "read the error in this image",
            "analyse this picture of my desktop",
            "describe what you see in this screenshot",
            "what does this image show",
        ),
    ),
    # ── Browser / web ───────────────────────────────────────────────────────
    (
        IntentName.NAVIGATE,
        (
            "go to youtube",
            "open youtube.com",
            "visit the github website",
            "take me to gmail",
            "open my bank site",
        ),
    ),
    (
        IntentName.SEARCH_WEB,
        (
            "search the web for python tutorials",
            "google the best laptops",
            "look up the weather forecast",
            "search online for local restaurants",
        ),
    ),
    (
        IntentName.BROWSER_ACTION,
        (
            "open chrome and search youtube for the latest ai news",
            "open the browser and search for flights to tokyo",
            "launch chrome and look up that error message",
            "open a new tab and google it",
        ),
    ),
    (
        IntentName.FILL_FORM,
        (
            "fill the form with my details",
            "complete the signup form",
            "fill out the contact form",
        ),
    ),
    (
        IntentName.EXTRACT_PAGE,
        (
            "extract the prices from this page",
            "scrape the table on this website",
            "read the page and list the links",
        ),
    ),
    # ── Knowledge / language ────────────────────────────────────────────────
    (
        IntentName.ANSWER_QUESTION,
        (
            "what is quantum computing",
            "explain how dns works",
            "why is the sky blue",
            "tell me about the roman empire",
            "who wrote hamlet",
        ),
    ),
    (
        IntentName.GENERAL_QUESTION,
        (
            "can you answer a quick question",
            "i have a question about subscriptions",
            "quick question for you",
        ),
    ),
    (
        IntentName.RESEARCH,
        (
            "research the best crm software",
            "look into electric cars for me",
            "find out about noise cancelling headphones",
            "dig into the history of this company",
        ),
    ),
    (
        IntentName.SUMMARIZE,
        (
            "summarize this article",
            "give me a summary of the meeting notes",
            "tldr this document",
        ),
    ),
    (
        IntentName.COMPARE,
        (
            "compare python and javascript",
            "what is the difference between ssd and hdd",
            "versus the pro model",
        ),
    ),
    (
        IntentName.CALCULATE,
        (
            "calculate 15% of 240",
            "compute 45 times 12",
            "what is the total of 12 plus 30",
            "divide 144 by 12",
        ),
    ),
    (
        IntentName.CONVERSATION,
        (
            "hello",
            "hey there",
            "good morning",
            "thanks that helped",
            "how are you doing",
        ),
    ),
    # ── Code / project ─────────────────────────────────────────────────────
    (
        IntentName.CODE_GENERATION,
        (
            "write a python function to sort a list",
            "generate a class for handling users",
            "create a script that renames files",
            "implement a binary search",
        ),
    ),
    (
        IntentName.CODE_EXPLANATION,
        (
            "explain this function",
            "what does this code do",
            "walk me through this algorithm",
            "why is this regex written that way",
        ),
    ),
    (
        IntentName.CODE_DEBUGGING,
        (
            "debug this traceback",
            "why is my test failing",
            "fix this error in my code",
            "find the bug in this function",
        ),
    ),
    (
        IntentName.PROJECT_ANALYSIS,
        (
            "analyse my project structure",
            "review the codebase",
            "what does this repository do",
            "give me an overview of the project",
        ),
    ),
    # ── Planning / memory / automation ─────────────────────────────────────
    (
        IntentName.PLAN_TASK,
        (
            "plan my week",
            "make a plan for the launch",
            "break this down into steps",
            "create a roadmap for the migration",
        ),
    ),
    (
        IntentName.REMEMBER,
        (
            "remember that i prefer dark mode",
            "keep in mind my meeting is on tuesday",
            "note that the wifi password is at work",
        ),
    ),
    (
        IntentName.RECALL,
        (
            "what did i tell you about my preferences",
            "recall the wifi password",
            "when did i last work on this",
        ),
    ),
    (
        IntentName.CHECK_STATUS,
        (
            "what is your status",
            "are you working",
            "give me a status report",
        ),
    ),
    (
        IntentName.RUN_COMMAND,
        (
            "run the tests",
            "execute the build script",
            "run command git status",
            "start the dev server command",
        ),
    ),
    (
        IntentName.TYPE_TEXT,
        (
            "type hello world",
            "enter my email into the field",
            "write this sentence for me",
        ),
    ),
    (
        IntentName.PRESS_KEY,
        (
            "press enter",
            "hit control s",
            "press the escape key",
        ),
    ),
)


def default_exemplars() -> tuple[tuple[IntentName, tuple[str, ...]], ...]:
    """The exemplar corpus for the lexical matcher."""
    return _EXEMPLARS


def exemplar_count() -> int:
    """Total number of indexed phrasings (used by telemetry and tests)."""
    return sum(len(phrases) for _intent, phrases in _EXEMPLARS)
