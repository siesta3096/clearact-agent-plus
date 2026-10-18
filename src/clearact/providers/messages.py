from clearact.domain.models import ChatMessage


def system_first_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Adapt runtime notes to chat templates requiring one leading system message.

    Only system-role content is combined. Preserve user/tool roles, tool-call
    transactions and the original ledger, including the positions of its notes.
    """
    notes = [message for message in messages if message.role == "system"]
    if not notes or (len(notes) == 1 and messages[0].role == "system"):
        return messages
    combined = notes[0].model_copy(update={"content": "\n\n".join(note.content or "" for note in notes)})
    return [combined, *(message for message in messages if message.role != "system")]
