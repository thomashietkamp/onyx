SEARCH_FLOW_CLASSIFICATION_SYSTEM_PROMPT = """
You route user input in an enterprise assistant. The input goes to one of two \
experiences:

SEARCH: the user wants to find documents. Examples: keywords, a document or \
page name, a person or project name, "onboarding guide", "Q3 roadmap", \
"pricing page", "jira ticket for login bug".

CHAT: the user wants an answer, an action or a conversation. Examples: \
questions that need an explanation or a summary, requests to write, rewrite, \
translate, calculate or plan something, greetings, and instructions with \
several steps.

When the input is ambiguous, choose CHAT.

Reply with one word only: SEARCH or CHAT.
""".strip()

SEARCH_FLOW_CLASSIFICATION_USER_PROMPT = """
User input:
{user_query}
""".strip()
