"""Telegram-бот: задачи на конкретный день, напоминания и трекер привычек.

Код собран по исходному промпту лабораторной 1.
Токен берётся из переменной TELEGRAM_BOT_TOKEN, данные чатов — из tasks.json.
"""

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

# Москва нужна и для «сегодня», и для просрочки, и для времени напоминания.
TZ = ZoneInfo("Europe/Moscow")
DATA_PATH = Path(__file__).resolve().with_name("tasks.json")

PRIORITY_WORDS = {
    "высокий": "high",
    "высокая": "high",
    "средний": "medium",
    "средняя": "medium",
    "низкий": "low",
    "низкая": "low",
}
PRIORITY_LABEL = {"high": "высокий", "medium": "средний", "low": "низкий"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

DATE_HINT = "Дата должна быть в виде ДД.ММ или ДД.ММ.ГГГГ, например 15.10."
TIME_HINT = "Время должно быть в виде ЧЧ:ММ, например 18:30."

HELP = """Я веду задачи и привычки. Основной способ — кнопки внизу экрана.

Сегодня — дела на этот день
Все задачи — всё, что ещё не закрыто
Просрочено — день уже прошёл
Новая задача — бот спросит текст, затем приоритет и день
Привычки — отметить, снять отметку или добавить
Статистика — выполненные, отменённые, просроченные и привычки

Под списком задач у каждого номера есть кнопки:
✅ выполнена, ❌ отменена, 🔔 напоминание, 📅 день и приоритет.

Команды /add, /today, /list, /done и остальные тоже работают.
"""

BTN_TODAY = "Сегодня"
BTN_LIST = "Все задачи"
BTN_OVERDUE = "Просрочено"
BTN_NEW = "Новая задача"
BTN_HABITS = "Привычки"
BTN_STATS = "Статистика"
TASK_LEGEND = "Кнопки с номером задачи: ✅ выполнена, ❌ отменена, 🔔 напоминание, 📅 день и приоритет."
PRIORITY_BY_CODE = {"high": "высокий", "medium": "средний", "low": "низкий"}

MENU = [
    ("start", "Что умеет бот"),
    ("help", "Список команд"),
    ("add", "Новая задача"),
    ("date", "Назначить день задачи"),
    ("today", "Задачи на сегодня"),
    ("day", "Задачи на выбранный день"),
    ("list", "Все активные задачи"),
    ("priority", "Сменить приоритет"),
    ("remind", "Поставить напоминание"),
    ("done", "Отметить задачу выполненной"),
    ("cancel", "Отменить задачу"),
    ("delete", "Удалить задачу"),
    ("overdue", "Просроченные задачи"),
    ("stats", "Статистика задач и привычек"),
    ("habit", "Новая привычка"),
    ("habits", "Список привычек"),
    ("check", "Отметить привычку за сегодня"),
    ("uncheck", "Снять отметку за сегодня"),
    ("habitstats", "Статистика привычек"),
]

logger = logging.getLogger("task_bot")


class StorageError(Exception):
    """Ошибка данных, которую можно показать пользователю без падения бота."""


def now_dt() -> datetime:
    """Текущий момент в Москве. В проверках эту функцию подменяют."""
    return datetime.now(TZ)


def today() -> date:
    return now_dt().date()


class Storage:
    """JSON-файл с задачами и привычками. У каждого чата свой раздел."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict:
        """Прочитать файл. Повреждённый JSON не перезаписывается."""
        if not self.path.exists():
            return {"chats": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.exception("Файл tasks.json повреждён, он не перезаписан")
            raise StorageError(
                "Файл данных повреждён. Я не стал его перезаписывать."
            ) from exc
        except OSError as exc:
            logger.exception("Не удалось прочитать tasks.json")
            raise StorageError("Не получилось прочитать данные. Попробуйте ещё раз.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("chats"), dict):
            logger.error("В tasks.json нет объекта chats, файл не перезаписан")
            raise StorageError("Файл данных повреждён. Я не стал его перезаписывать.")
        return data

    def save(self, data: dict) -> None:
        """Записать файл целиком через временный файл, чтобы не оставить обрывок."""
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            logger.exception("Не удалось записать tasks.json")
            raise StorageError("Не получилось сохранить данные. Попробуйте ещё раз.") from exc

    def ensure_chat(self, data: dict, chat_id: int) -> dict:
        key = str(chat_id)
        chat = data["chats"].get(key)
        if chat is None:
            chat = {"next_task_id": 1, "tasks": [], "habits": []}
            data["chats"][key] = chat
        if not isinstance(chat, dict) or not isinstance(chat.get("tasks"), list):
            raise StorageError("Файл данных повреждён. Я не стал его перезаписывать.")
        if not isinstance(chat.get("habits"), list):
            raise StorageError("Файл данных повреждён. Я не стал его перезаписывать.")
        ids = [task.get("id") for task in chat["tasks"] if isinstance(task, dict)]
        numeric = [item for item in ids if isinstance(item, int)]
        nxt = chat.get("next_task_id", 1)
        if not isinstance(nxt, int):
            nxt = 1
        if numeric:
            nxt = max(nxt, max(numeric) + 1)
        chat["next_task_id"] = nxt
        return chat

    def read_chat(self, data: dict, chat_id: int) -> dict | None:
        chat = data["chats"].get(str(chat_id))
        if chat is None:
            return None
        if not isinstance(chat, dict) or not isinstance(chat.get("tasks"), list):
            raise StorageError("Файл данных повреждён. Я не стал его перезаписывать.")
        if not isinstance(chat.get("habits"), list):
            raise StorageError("Файл данных повреждён. Я не стал его перезаписывать.")
        return chat


store = Storage(DATA_PATH)


def parse_date_token(token: str) -> date:
    match = DATE_RE.fullmatch(token.strip())
    if not match:
        raise ValueError(DATE_HINT)
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else today().year
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(DATE_HINT) from exc


def parse_time_token(token: str) -> tuple[int, int]:
    match = TIME_RE.fullmatch(token.strip())
    if not match:
        raise ValueError(TIME_HINT)
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(TIME_HINT)
    return hour, minute


def parse_task_id(token: str) -> int:
    if not token or not token.isdigit():
        raise ValueError("Номер задачи должен быть целым числом, например 3.")
    return int(token)


def format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def parse_stored_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def reminder_name(chat_id: int, task_id: int) -> str:
    return f"remind-{chat_id}-{task_id}"


def cancel_reminder(job_queue, chat_id: int, task_id: int) -> None:
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(reminder_name(chat_id, task_id)):
        job.schedule_removal()


def schedule_reminder(job_queue, chat_id: int, task: dict) -> None:
    """Поставить одно будущее напоминание. Прошедшее время при старте не отправляется."""
    if job_queue is None or not isinstance(task, dict):
        return
    task_id = task.get("id")
    if not isinstance(task_id, int):
        return
    if task.get("status") != "active" or not task.get("remind_at"):
        cancel_reminder(job_queue, chat_id, task_id)
        return
    try:
        when = datetime.fromisoformat(task["remind_at"])
    except (TypeError, ValueError):
        logger.exception("Некорректное время напоминания у задачи %s", task_id)
        return
    if when.tzinfo is None:
        when = when.replace(tzinfo=TZ)
    if when <= now_dt():
        return
    cancel_reminder(job_queue, chat_id, task_id)
    job_queue.run_once(
        send_reminder,
        when=when,
        data={"chat_id": chat_id, "task_id": task_id, "remind_at": task["remind_at"]},
        name=reminder_name(chat_id, task_id),
        chat_id=chat_id,
    )


def find_task(chat: dict, task_id: int) -> dict | None:
    for task in chat["tasks"]:
        if isinstance(task, dict) and task.get("id") == task_id:
            return task
    return None


def find_habit(chat: dict, name: str) -> dict | None:
    needle = name.casefold()
    for habit in chat["habits"]:
        if isinstance(habit, dict) and str(habit.get("name", "")).casefold() == needle:
            return habit
    return None


def active_message(task: dict) -> str | None:
    """Текст отказа, если задачу уже нельзя менять."""
    status = task.get("status")
    if status == "active":
        return None
    if status == "done":
        return f"Задача #{task['id']} уже выполнена."
    if status == "cancelled":
        return f"Задача #{task['id']} отменена."
    return f"Задача #{task['id']} недоступна."


def format_task(task: dict, with_date: bool) -> str:
    label = PRIORITY_LABEL.get(task.get("priority"), "средний")
    line = f"#{task['id']} [{label}] {task.get('text', '')}"
    extra = []
    due = parse_stored_date(task.get("due"))
    if with_date and due is not None:
        extra.append(format_date(due))
    remind_at = task.get("remind_at")
    if remind_at:
        try:
            moment = datetime.fromisoformat(remind_at)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=TZ)
            extra.append("напоминание " + moment.strftime("%d.%m %H:%M"))
        except (TypeError, ValueError):
            extra.append("напоминание не распознано")
    if extra:
        return line + " — " + ", ".join(extra)
    return line


def sort_tasks(tasks: list[dict]) -> list[dict]:
    return sorted(
        tasks,
        key=lambda task: (PRIORITY_ORDER.get(task.get("priority"), 1), task.get("id", 0)),
    )


def active_tasks(chat: dict) -> list[dict]:
    return [task for task in chat["tasks"] if isinstance(task, dict) and task.get("status") == "active"]


def is_overdue(task: dict, day: date) -> bool:
    due = parse_stored_date(task.get("due"))
    return task.get("status") == "active" and due is not None and due < day


def habit_marks(habit: dict) -> set[date]:
    marks = set()
    for raw in habit.get("marks", []):
        try:
            marks.add(date.fromisoformat(raw))
        except (TypeError, ValueError):
            continue
    return marks


def current_streak(marks: set[date], day: date) -> int:
    """Серия до сегодня. Если сегодня пусто, но вчера отмечено, серия ещё жива."""
    if day in marks:
        end = day
    elif (day - timedelta(days=1)) in marks:
        end = day - timedelta(days=1)
    else:
        return 0
    streak = 0
    cursor = end
    while cursor in marks:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def best_streak(marks: set[date]) -> int:
    if not marks:
        return 0
    days = sorted(marks)
    best = 1
    current = 1
    for index in range(1, len(days)):
        if days[index] == days[index - 1] + timedelta(days=1):
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


def marks_in_window(marks: set[date], day: date, window: int) -> int:
    start = day - timedelta(days=window - 1)
    return sum(1 for item in marks if start <= item <= day)


def percent(part: int, whole: int) -> int:
    if whole == 0:
        return 0
    return round(part * 100 / whole)


def split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buffer = ""
    for line in text.split("\n"):
        candidate = line if not buffer else buffer + "\n" + line
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            parts.append(buffer)
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        buffer = line
    if buffer:
        parts.append(buffer)
    return parts


def render_task_blocks(title: str, tasks: list[dict], with_date: bool, empty: str) -> str:
    if not tasks:
        return empty
    lines = [title, ""]
    lines.extend(format_task(task, with_date=with_date) for task in tasks)
    return "\n".join(lines)


def usage_add() -> str:
    return "Напишите задачу. Пример: /add высокий отчёт 15.10"


def cmd_add(chat_id: int, args: list[str], job_queue) -> str:
    if not args:
        return usage_add()
    tokens = list(args)
    priority = "medium"
    if tokens[0].casefold() in PRIORITY_WORDS:
        priority = PRIORITY_WORDS[tokens.pop(0).casefold()]
    due = None
    if tokens and DATE_RE.fullmatch(tokens[-1]):
        try:
            due = parse_date_token(tokens.pop())
        except ValueError as exc:
            return str(exc)
    text = " ".join(tokens).strip()
    if not text:
        return usage_add()

    data = store.load()
    chat = store.ensure_chat(data, chat_id)
    task_id = chat["next_task_id"]
    task = {
        "id": task_id,
        "text": text,
        "priority": priority,
        "due": due.isoformat() if due else None,
        "remind_at": None,
        "status": "active",
        "created_at": now_dt().isoformat(),
    }
    chat["tasks"].append(task)
    chat["next_task_id"] = task_id + 1
    store.save(data)

    day_text = format_date(due) if due else "не назначен"
    return (
        f"Добавил задачу #{task_id}: {text}. "
        f"Приоритет: {PRIORITY_LABEL[priority]}. День: {day_text}."
    )


def require_task(chat: dict | None, task_id: int) -> dict:
    if chat is None or (task := find_task(chat, task_id)) is None:
        raise LookupError(f"Задачи с номером {task_id} нет.")
    return task


def cmd_date(chat_id: int, args: list[str], job_queue) -> str:
    if len(args) != 2:
        return "Укажите номер и день. Пример: /date 3 15.10"
    try:
        task_id = parse_task_id(args[0])
        due = parse_date_token(args[1])
    except ValueError as exc:
        return str(exc)
    data = store.load()
    try:
        task = require_task(store.read_chat(data, chat_id), task_id)
    except LookupError as exc:
        return str(exc)
    if message := active_message(task):
        return message
    task["due"] = due.isoformat()
    store.save(data)
    return f"Задача #{task_id} назначена на {format_date(due)}."


def cmd_today(chat_id: int, args: list[str], job_queue) -> str:
    day = today()
    data = store.load()
    chat = store.read_chat(data, chat_id)
    tasks = []
    if chat is not None:
        tasks = [
            task
            for task in active_tasks(chat)
            if parse_stored_date(task.get("due")) == day
        ]
    return render_task_blocks(
        f"На сегодня, {format_date(day)}",
        sort_tasks(tasks),
        with_date=False,
        empty="На сегодня задач нет.",
    )


def cmd_day(chat_id: int, args: list[str], job_queue) -> str:
    if len(args) != 1:
        return "Укажите день. Пример: /day 15.10"
    try:
        day = parse_date_token(args[0])
    except ValueError as exc:
        return str(exc)
    data = store.load()
    chat = store.read_chat(data, chat_id)
    tasks = []
    if chat is not None:
        tasks = [
            task
            for task in active_tasks(chat)
            if parse_stored_date(task.get("due")) == day
        ]
    return render_task_blocks(
        f"На {format_date(day)}",
        sort_tasks(tasks),
        with_date=False,
        empty=f"На {format_date(day)} задач нет.",
    )


def cmd_list(chat_id: int, args: list[str], job_queue) -> str:
    data = store.load()
    chat = store.read_chat(data, chat_id)
    if chat is None:
        return "Активных задач нет."
    tasks = active_tasks(chat)
    if not tasks:
        return "Активных задач нет."
    inbox = sort_tasks([task for task in tasks if not task.get("due")])
    dated = [task for task in tasks if task.get("due")]
    lines = []
    if inbox:
        lines.append("Входящие")
        lines.extend(format_task(task, with_date=False) for task in inbox)
    groups: dict[str, list[dict]] = {}
    for task in dated:
        groups.setdefault(task["due"], []).append(task)
    for due in sorted(groups):
        if lines:
            lines.append("")
        lines.append(format_date(date.fromisoformat(due)))
        lines.extend(format_task(task, with_date=False) for task in sort_tasks(groups[due]))
    return "\n".join(lines)


def cmd_priority(chat_id: int, args: list[str], job_queue) -> str:
    if len(args) != 2:
        return "Укажите номер и приоритет. Пример: /priority 3 высокий"
    try:
        task_id = parse_task_id(args[0])
    except ValueError as exc:
        return str(exc)
    priority = PRIORITY_WORDS.get(args[1].casefold())
    if priority is None:
        return "Приоритет: высокий, средний или низкий."
    data = store.load()
    try:
        task = require_task(store.read_chat(data, chat_id), task_id)
    except LookupError as exc:
        return str(exc)
    if message := active_message(task):
        return message
    task["priority"] = priority
    store.save(data)
    return f"У задачи #{task_id} теперь приоритет «{PRIORITY_LABEL[priority]}»."


def cmd_remind(chat_id: int, args: list[str], job_queue) -> str:
    if len(args) not in (2, 3):
        return "Пример: /remind 3 18:30 или /remind 3 15.10 09:00"
    try:
        task_id = parse_task_id(args[0])
        if len(args) == 2:
            day = today()
            hour, minute = parse_time_token(args[1])
        else:
            day = parse_date_token(args[1])
            hour, minute = parse_time_token(args[2])
    except ValueError as exc:
        return str(exc)
    moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    if moment <= now_dt():
        return "Это время уже прошло. Укажите будущее время."

    data = store.load()
    try:
        task = require_task(store.read_chat(data, chat_id), task_id)
    except LookupError as exc:
        return str(exc)
    if message := active_message(task):
        return message
    task["remind_at"] = moment.isoformat()
    store.save(data)
    schedule_reminder(job_queue, chat_id, task)
    return f"Напомню про задачу #{task_id} {moment.strftime('%d.%m.%Y в %H:%M')}."


def change_status(chat_id: int, args: list[str], job_queue, status: str, success: str) -> str:
    if len(args) != 1:
        example = "/done 3" if status == "done" else "/cancel 3"
        return f"Укажите номер задачи. Пример: {example}"
    try:
        task_id = parse_task_id(args[0])
    except ValueError as exc:
        return str(exc)
    data = store.load()
    try:
        task = require_task(store.read_chat(data, chat_id), task_id)
    except LookupError as exc:
        return str(exc)
    if message := active_message(task):
        return message
    task["status"] = status
    store.save(data)
    cancel_reminder(job_queue, chat_id, task_id)
    return success.format(id=task_id, text=task.get("text", ""))


def cmd_done(chat_id: int, args: list[str], job_queue) -> str:
    return change_status(chat_id, args, job_queue, "done", "Задача #{id} выполнена: {text}")


def cmd_cancel(chat_id: int, args: list[str], job_queue) -> str:
    return change_status(chat_id, args, job_queue, "cancelled", "Задача #{id} отменена: {text}")


def cmd_delete(chat_id: int, args: list[str], job_queue) -> str:
    if len(args) != 1:
        return "Укажите номер задачи. Пример: /delete 3"
    try:
        task_id = parse_task_id(args[0])
    except ValueError as exc:
        return str(exc)
    data = store.load()
    chat = store.read_chat(data, chat_id)
    try:
        task = require_task(chat, task_id)
    except LookupError as exc:
        return str(exc)
    chat["tasks"] = [item for item in chat["tasks"] if item.get("id") != task_id]
    store.save(data)
    cancel_reminder(job_queue, chat_id, task_id)
    return f"Задача #{task_id} удалена."


def cmd_overdue(chat_id: int, args: list[str], job_queue) -> str:
    day = today()
    data = store.load()
    chat = store.read_chat(data, chat_id)
    tasks = []
    if chat is not None:
        tasks = [task for task in chat["tasks"] if isinstance(task, dict) and is_overdue(task, day)]
    tasks.sort(key=lambda task: (task.get("due") or "", PRIORITY_ORDER.get(task.get("priority"), 1)))
    return render_task_blocks("Просроченные", tasks, with_date=True, empty="Просроченных задач нет.")


def cmd_stats(chat_id: int, args: list[str], job_queue) -> str:
    day = today()
    data = store.load()
    chat = store.read_chat(data, chat_id)
    tasks = chat["tasks"] if chat else []
    habits = chat["habits"] if chat else []
    active = sum(1 for task in tasks if isinstance(task, dict) and task.get("status") == "active")
    done = sum(1 for task in tasks if isinstance(task, dict) and task.get("status") == "done")
    cancelled = sum(1 for task in tasks if isinstance(task, dict) and task.get("status") == "cancelled")
    overdue = sum(1 for task in tasks if isinstance(task, dict) and is_overdue(task, day))
    marked_today = 0
    for habit in habits:
        if isinstance(habit, dict) and day.isoformat() in habit.get("marks", []):
            marked_today += 1
    return "\n".join(
        [
            "Задачи",
            f"Активные: {active}",
            f"Выполненные: {done}",
            f"Отменённые: {cancelled}",
            f"Просроченные: {overdue}",
            "",
            "Просроченные уже входят в число активных.",
            "",
            "Привычки",
            f"Всего: {len(habits)}",
            f"Отмечено сегодня: {marked_today}",
        ]
    )


def habit_name(args: list[str]) -> str:
    return " ".join(args).strip()


def cmd_habit(chat_id: int, args: list[str], job_queue) -> str:
    name = habit_name(args)
    if not name:
        return "Напишите название. Пример: /habit зарядка"
    data = store.load()
    chat = store.ensure_chat(data, chat_id)
    if find_habit(chat, name) is not None:
        return "Такая привычка уже есть."
    chat["habits"].append({"name": name, "marks": []})
    store.save(data)
    return f"Добавил привычку «{name}». Отметить её можно в разделе «Привычки»."


def cmd_habits(chat_id: int, args: list[str], job_queue) -> str:
    data = store.load()
    chat = store.read_chat(data, chat_id)
    habits = chat["habits"] if chat else []
    if not habits:
        return "Привычек пока нет. Создайте: /habit зарядка"
    day = today()
    lines = ["Привычки", ""]
    for habit in habits:
        marks = habit_marks(habit)
        mark = "✓" if day in marks else "•"
        lines.append(f"{mark} {habit.get('name')} — серия {current_streak(marks, day)}")
    return "\n".join(lines)


def cmd_check(chat_id: int, args: list[str], job_queue) -> str:
    name = habit_name(args)
    if not name:
        return "Напишите название. Пример: /check зарядка"
    data = store.load()
    chat = store.read_chat(data, chat_id)
    habit = find_habit(chat, name) if chat else None
    if habit is None:
        return f"Привычки «{name}» нет. Создайте её: /habit {name}"
    day = today()
    marks = habit.setdefault("marks", [])
    if day.isoformat() in marks:
        return f"Сегодня «{habit['name']}» уже отмечена."
    marks.append(day.isoformat())
    marks.sort()
    store.save(data)
    streak = current_streak(habit_marks(habit), day)
    return f"Отметил «{habit['name']}» за сегодня. Серия: {streak}."


def cmd_uncheck(chat_id: int, args: list[str], job_queue) -> str:
    name = habit_name(args)
    if not name:
        return "Напишите название. Пример: /uncheck зарядка"
    data = store.load()
    chat = store.read_chat(data, chat_id)
    habit = find_habit(chat, name) if chat else None
    if habit is None:
        return f"Привычки «{name}» нет."
    day = today().isoformat()
    marks = habit.get("marks", [])
    if day not in marks:
        return f"Сегодня «{habit['name']}» ещё не отмечена."
    habit["marks"] = [item for item in marks if item != day]
    store.save(data)
    return f"Снял отметку «{habit['name']}» за сегодня."


def cmd_habitstats(chat_id: int, args: list[str], job_queue) -> str:
    data = store.load()
    chat = store.read_chat(data, chat_id)
    habits = chat["habits"] if chat else []
    if not habits:
        return "Привычек пока нет. Создайте: /habit зарядка"
    day = today()
    blocks = []
    for habit in habits:
        marks = habit_marks(habit)
        week = marks_in_window(marks, day, 7)
        month = marks_in_window(marks, day, 30)
        blocks.append(
            "\n".join(
                [
                    str(habit.get("name")),
                    f"Отмечено дней: {len(marks)}",
                    f"Текущая серия: {current_streak(marks, day)}",
                    f"Лучшая серия: {best_streak(marks)}",
                    f"За 7 дней: отмечено {week} из 7 ({percent(week, 7)}%)",
                    f"За 30 дней: отмечено {month} из 30 ({percent(month, 30)}%)",
                ]
            )
        )
    return "\n\n".join(blocks)


def main_keyboard() -> ReplyKeyboardMarkup:
    """Нижняя клавиатура. Она остаётся на экране, пока пользователь её не скроет."""
    return ReplyKeyboardMarkup(
        [
            [BTN_TODAY, BTN_LIST, BTN_OVERDUE],
            [BTN_NEW, BTN_HABITS, BTN_STATS],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def run_action(chat_id: int, action, args: list[str], job_queue) -> str:
    try:
        return action(chat_id, args, job_queue)
    except StorageError as exc:
        logger.exception("Ошибка хранилища")
        return str(exc)
    except Exception:
        logger.exception("Необработанная ошибка команды")
        return "Не получилось выполнить команду. Попробуйте ещё раз."


def load_chat(chat_id: int) -> dict | None:
    data = store.load()
    return store.read_chat(data, chat_id)


def ordered_active(chat_id: int) -> list[dict]:
    """Активные задачи в том же порядке, что и в общем списке."""
    chat = load_chat(chat_id)
    if chat is None:
        return []
    tasks = active_tasks(chat)
    inbox = sort_tasks([task for task in tasks if not task.get("due")])
    groups: dict[str, list[dict]] = {}
    for task in tasks:
        if task.get("due"):
            groups.setdefault(task["due"], []).append(task)
    ordered = list(inbox)
    for due in sorted(groups):
        ordered.extend(sort_tasks(groups[due]))
    return ordered


def tasks_on_day(chat_id: int, day: date) -> list[dict]:
    return sort_tasks(
        [task for task in ordered_active(chat_id) if parse_stored_date(task.get("due")) == day]
    )


def tasks_overdue(chat_id: int) -> list[dict]:
    chat = load_chat(chat_id)
    if chat is None:
        return []
    day = today()
    tasks = [task for task in chat["tasks"] if isinstance(task, dict) and is_overdue(task, day)]
    tasks.sort(key=lambda task: (task.get("due") or "", PRIORITY_ORDER.get(task.get("priority"), 1)))
    return tasks


def load_habits(chat_id: int) -> list[dict]:
    chat = load_chat(chat_id)
    if chat is None:
        return []
    return [habit for habit in chat["habits"] if isinstance(habit, dict)]


def task_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup | None:
    """По строке кнопок на задачу. Дальше 12 задач остаются в тексте без кнопок."""
    rows = []
    for task in tasks[:12]:
        task_id = task["id"]
        rows.append(
            [
                InlineKeyboardButton(f"✅ {task_id}", callback_data=f"done:{task_id}"),
                InlineKeyboardButton(f"❌ {task_id}", callback_data=f"cancel:{task_id}"),
                InlineKeyboardButton(f"🔔 {task_id}", callback_data=f"rem:{task_id}"),
                InlineKeyboardButton(f"📅 {task_id}", callback_data=f"date:{task_id}"),
            ]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


def habit_keyboard(habits: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for index, habit in enumerate(habits[:15]):
        name = str(habit.get("name", ""))
        short = name if len(name) <= 20 else name[:19] + "…"
        rows.append(
            [
                InlineKeyboardButton(f"✓ {short}", callback_data=f"hcheck:{index}"),
                InlineKeyboardButton("Снять", callback_data=f"huncheck:{index}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("Новая привычка", callback_data="hnew"),
            InlineKeyboardButton("Статистика", callback_data="hstats"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def date_menu(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Сегодня", callback_data=f"setdate:{task_id}:today"),
                InlineKeyboardButton("Завтра", callback_data=f"setdate:{task_id}:tom"),
                InlineKeyboardButton("Свой день", callback_data=f"setdate:{task_id}:ask"),
            ],
            [
                InlineKeyboardButton("Высокий", callback_data=f"setprio:{task_id}:high"),
                InlineKeyboardButton("Средний", callback_data=f"setprio:{task_id}:medium"),
                InlineKeyboardButton("Низкий", callback_data=f"setprio:{task_id}:low"),
            ],
            [InlineKeyboardButton("Удалить", callback_data=f"delask:{task_id}")],
        ]
    )


def remind_menu(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Через час", callback_data=f"setrem:{task_id}:1h"),
                InlineKeyboardButton("Сегодня 18:00", callback_data=f"setrem:{task_id}:18"),
                InlineKeyboardButton("Завтра 09:00", callback_data=f"setrem:{task_id}:tom9"),
            ],
            [InlineKeyboardButton("Своё время", callback_data=f"setrem:{task_id}:ask")],
        ]
    )


def priority_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Высокий", callback_data="newprio:high"),
                InlineKeyboardButton("Средний", callback_data="newprio:medium"),
                InlineKeyboardButton("Низкий", callback_data="newprio:low"),
            ]
        ]
    )


def new_date_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Сегодня", callback_data="newdate:today"),
                InlineKeyboardButton("Завтра", callback_data="newdate:tom"),
            ],
            [
                InlineKeyboardButton("Без даты", callback_data="newdate:none"),
                InlineKeyboardButton("Свой день", callback_data="newdate:ask"),
            ]
        ]
    )


def build_add_args(text: str, priority: str, due: date | None) -> list[str]:
    args = [PRIORITY_BY_CODE[priority], *text.split()]
    if due is not None:
        args.append(due.strftime("%d.%m.%Y"))
    return args


def clear_draft(user_data: dict) -> None:
    user_data.pop("await", None)
    user_data.pop("draft_text", None)
    user_data.pop("draft_priority", None)


def create_draft_task(chat_id: int, user_data: dict, due: date | None, job_queue) -> str:
    text = str(user_data.get("draft_text", "")).strip()
    priority = str(user_data.get("draft_priority", "medium"))
    if not text or priority not in PRIORITY_BY_CODE:
        return "Сначала напишите текст задачи через кнопку «Новая задача»."
    result = run_action(chat_id, cmd_add, build_add_args(text, priority, due), job_queue)
    clear_draft(user_data)
    return result


def list_footer(text: str, tasks: list[dict]) -> str:
    if not tasks:
        return text
    extra = TASK_LEGEND
    if len(tasks) > 12:
        extra += " Кнопки показаны для первых 12 задач."
    return text + "\n\n" + extra


ACTIONS = {
    "add": cmd_add,
    "date": cmd_date,
    "today": cmd_today,
    "day": cmd_day,
    "list": cmd_list,
    "priority": cmd_priority,
    "remind": cmd_remind,
    "done": cmd_done,
    "cancel": cmd_cancel,
    "delete": cmd_delete,
    "overdue": cmd_overdue,
    "stats": cmd_stats,
    "habit": cmd_habit,
    "habits": cmd_habits,
    "check": cmd_check,
    "uncheck": cmd_uncheck,
    "habitstats": cmd_habitstats,
}


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправить напоминание, только если задача всё ещё активна и время не меняли."""
    payload = context.job.data or {}
    chat_id = payload.get("chat_id")
    task_id = payload.get("task_id")
    expected = payload.get("remind_at")
    try:
        data = store.load()
        chat = store.read_chat(data, chat_id)
        task = find_task(chat, task_id) if chat else None
    except StorageError:
        logger.exception("Не удалось прочитать данные для напоминания")
        return
    if task is None or task.get("status") != "active" or task.get("remind_at") != expected:
        return
    await context.bot.send_message(chat_id=chat_id, text=f"Напоминание: {task.get('text', '')}")


async def post_init(application: Application) -> None:
    """После запуска заново поставить будущие напоминания и меню команд."""
    job_queue = application.job_queue
    if job_queue is None:
        logger.error("JobQueue недоступен. Установите python-telegram-bot[job-queue].")
    else:
        try:
            data = store.load()
        except StorageError:
            logger.exception("Не удалось восстановить напоминания")
        else:
            for chat_key, chat in data.get("chats", {}).items():
                if not isinstance(chat, dict) or not str(chat_key).lstrip("-").isdigit():
                    continue
                for task in chat.get("tasks", []):
                    schedule_reminder(job_queue, int(chat_key), task)
    await application.bot.set_my_commands([BotCommand(name, description) for name, description in MENU])


async def send_text(message, text: str, markup=None) -> None:
    parts = split_message(text)
    for index, part in enumerate(parts):
        await message.reply_text(part, reply_markup=markup if index == len(parts) - 1 else None)


def make_handler(action):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or update.effective_chat is None:
            return
        text = run_action(
            update.effective_chat.id,
            action,
            list(context.args or []),
            context.job_queue,
        )
        await send_text(message, text, main_keyboard())

    return handler


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(HELP, reply_markup=main_keyboard())


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            "Не знаю такую команду. Нажмите кнопку внизу или откройте /help",
            reply_markup=main_keyboard(),
        )


async def show_tasks(message, text: str, tasks: list[dict]) -> None:
    markup = task_keyboard(tasks)
    await send_text(message, list_footer(text, tasks), markup if markup is not None else main_keyboard())


async def dispatch_button(message, context: ContextTypes.DEFAULT_TYPE, chat_id: int, kind: str) -> None:
    try:
        await route_button(message, context, chat_id, kind)
    except StorageError as exc:
        logger.exception("Ошибка хранилища")
        await message.reply_text(str(exc))


async def route_button(message, context: ContextTypes.DEFAULT_TYPE, chat_id: int, kind: str) -> None:
    job_queue = context.job_queue
    if kind == "today":
        await show_tasks(message, run_action(chat_id, cmd_today, [], job_queue), tasks_on_day(chat_id, today()))
    elif kind == "list":
        await show_tasks(message, run_action(chat_id, cmd_list, [], job_queue), ordered_active(chat_id))
    elif kind == "overdue":
        await show_tasks(message, run_action(chat_id, cmd_overdue, [], job_queue), tasks_overdue(chat_id))
    elif kind == "stats":
        await send_text(message, run_action(chat_id, cmd_stats, [], job_queue), main_keyboard())
    elif kind == "habits":
        habits = load_habits(chat_id)
        text = "Привычек пока нет." if not habits else run_action(chat_id, cmd_habits, [], job_queue)
        await send_text(message, text, habit_keyboard(habits))
    elif kind == "new":
        context.user_data["await"] = {"kind": "task_text"}
        await message.reply_text("Напишите текст задачи одним сообщением.", reply_markup=main_keyboard())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or update.effective_chat is None or not message.text:
        return
    chat_id = update.effective_chat.id
    text = message.text.strip()
    buttons = {
        BTN_TODAY: "today",
        BTN_LIST: "list",
        BTN_OVERDUE: "overdue",
        BTN_NEW: "new",
        BTN_HABITS: "habits",
        BTN_STATS: "stats",
    }
    if text in buttons:
        clear_draft(context.user_data)
        await dispatch_button(message, context, chat_id, buttons[text])
        return

    waiting = context.user_data.get("await") or {}
    kind = waiting.get("kind")
    job_queue = context.job_queue
    if kind == "task_text":
        context.user_data["draft_text"] = text
        context.user_data["await"] = {"kind": "task_priority"}
        await message.reply_text("Выберите приоритет.", reply_markup=priority_menu())
        return
    if kind == "task_date":
        try:
            due = parse_date_token(text)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        await send_text(
            message,
            create_draft_task(chat_id, context.user_data, due, job_queue),
            main_keyboard(),
        )
        return
    if kind == "due":
        result = run_action(chat_id, cmd_date, [str(waiting.get("task_id")), text], job_queue)
        if not result.startswith("Дата должна") and not result.startswith("Укажите"):
            clear_draft(context.user_data)
        await send_text(message, result, main_keyboard())
        return
    if kind == "remind":
        result = run_action(
            chat_id,
            cmd_remind,
            [str(waiting.get("task_id")), *text.split()],
            job_queue,
        )
        if result.startswith("Напомню"):
            clear_draft(context.user_data)
        await send_text(message, result, main_keyboard())
        return
    if kind == "habit":
        result = run_action(chat_id, cmd_habit, [text], job_queue)
        if result.startswith("Добавил") or result.startswith("Такая привычка"):
            clear_draft(context.user_data)
        await send_text(message, result, main_keyboard())
        return

    await message.reply_text(
        "Нажмите кнопку внизу. Чтобы добавить задачу, выберите «Новая задача».",
        reply_markup=main_keyboard(),
    )


def callback_task_id(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return None


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_chat is None or query.message is None:
        return
    parts = query.data.split(":")
    action = parts[0]
    if action == "noop":
        await query.answer("Оставил как есть")
        return
    await query.answer()
    try:
        await route_callback(query, context, parts)
    except StorageError as exc:
        logger.exception("Ошибка хранилища")
        await query.message.reply_text(str(exc))


async def route_callback(query, context: ContextTypes.DEFAULT_TYPE, parts: list[str]) -> None:
    action = parts[0]
    chat_id = query.message.chat_id
    job_queue = context.job_queue
    message = query.message
    task_id = callback_task_id(parts[1]) if len(parts) > 1 else None

    if action == "done" and task_id is not None:
        await send_text(message, run_action(chat_id, cmd_done, [str(task_id)], job_queue))
    elif action == "cancel" and task_id is not None:
        await send_text(message, run_action(chat_id, cmd_cancel, [str(task_id)], job_queue))
    elif action == "del" and task_id is not None:
        await send_text(message, run_action(chat_id, cmd_delete, [str(task_id)], job_queue))
    elif action == "delask" and task_id is not None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Да, удалить", callback_data=f"del:{task_id}"),
                    InlineKeyboardButton("Нет", callback_data="noop"),
                ]
            ]
        )
        await message.reply_text(f"Удалить задачу #{task_id}? В статистике её не останется.", reply_markup=keyboard)
    elif action == "date" and task_id is not None:
        await message.reply_text(
            f"День и приоритет задачи #{task_id}. Удаление тоже здесь.",
            reply_markup=date_menu(task_id),
        )
    elif action == "rem" and task_id is not None:
        await message.reply_text(f"Когда напомнить про задачу #{task_id}?", reply_markup=remind_menu(task_id))
    elif action == "setprio" and task_id is not None and len(parts) == 3:
        word = PRIORITY_BY_CODE.get(parts[2])
        if word is None:
            await message.reply_text("Приоритет: высокий, средний или низкий.")
            return
        await send_text(message, run_action(chat_id, cmd_priority, [str(task_id), word], job_queue))
    elif action == "setdate" and task_id is not None and len(parts) == 3:
        choice = parts[2]
        if choice == "ask":
            context.user_data["await"] = {"kind": "due", "task_id": task_id}
            await message.reply_text("Напишите день, например 15.10")
            return
        due = today() if choice == "today" else today() + timedelta(days=1)
        if choice not in {"today", "tom"}:
            return
        await send_text(
            message,
            run_action(chat_id, cmd_date, [str(task_id), due.strftime("%d.%m.%Y")], job_queue),
        )
    elif action == "setrem" and task_id is not None and len(parts) == 3:
        choice = parts[2]
        if choice == "ask":
            context.user_data["await"] = {"kind": "remind", "task_id": task_id}
            await message.reply_text("Напишите время: 18:30 или 15.10 18:30")
            return
        if choice == "1h":
            moment = now_dt() + timedelta(hours=1)
            args = [str(task_id), moment.strftime("%d.%m.%Y"), moment.strftime("%H:%M")]
        elif choice == "18":
            args = [str(task_id), "18:00"]
        elif choice == "tom9":
            args = [str(task_id), (today() + timedelta(days=1)).strftime("%d.%m.%Y"), "09:00"]
        else:
            return
        await send_text(message, run_action(chat_id, cmd_remind, args, job_queue))
    elif action == "newprio" and len(parts) == 2:
        if parts[1] not in PRIORITY_BY_CODE or not context.user_data.get("draft_text"):
            await message.reply_text("Сначала напишите текст через кнопку «Новая задача».")
            return
        context.user_data["draft_priority"] = parts[1]
        context.user_data["await"] = {"kind": "task_date_choice"}
        await message.reply_text("На какой день поставить задачу?", reply_markup=new_date_menu())
    elif action == "newdate" and len(parts) == 2:
        choice = parts[1]
        if choice == "ask":
            context.user_data["await"] = {"kind": "task_date"}
            await message.reply_text("Напишите день, например 15.10")
            return
        due = None
        if choice == "today":
            due = today()
        elif choice == "tom":
            due = today() + timedelta(days=1)
        elif choice != "none":
            return
        await send_text(message, create_draft_task(chat_id, context.user_data, due, job_queue), main_keyboard())
    elif action == "hnew":
        context.user_data["await"] = {"kind": "habit"}
        await message.reply_text("Напишите название привычки.", reply_markup=main_keyboard())
    elif action == "hstats":
        await send_text(message, run_action(chat_id, cmd_habitstats, [], job_queue))
    elif action in {"hcheck", "huncheck"} and task_id is not None:
        habits = load_habits(chat_id)
        if task_id >= len(habits):
            await message.reply_text("Список привычек изменился. Нажмите «Привычки» ещё раз.")
            return
        command = cmd_check if action == "hcheck" else cmd_uncheck
        await send_text(message, run_action(chat_id, command, [str(habits[task_id].get("name", ""))], job_queue))
    else:
        await message.reply_text("Эта кнопка устарела. Откройте список ещё раз.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке обновления", exc_info=context.error)


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Укажите TELEGRAM_BOT_TOKEN в файле .env")

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.INFO,
    )
    # В адресе запросов есть токен, поэтому сами запросы в лог не пишем.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    application = (
        Application.builder()
        .token(token)
        .defaults(Defaults(tzinfo=TZ))
        .post_init(post_init)
        .build()
    )
    if application.job_queue is None:
        raise SystemExit("Нужна установка с очередью задач: python-telegram-bot[job-queue]")

    application.add_handler(CommandHandler(["start", "help"], help_command))
    for name, action in ACTIONS.items():
        application.add_handler(CommandHandler(name, make_handler(action)))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(on_error)
    application.run_polling()


if __name__ == "__main__":
    main()
