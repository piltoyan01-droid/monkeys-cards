"""Local token setup with clipboard support. Never contacts Telegram."""

from pathlib import Path
import re


def save_settings(username: str, token: str) -> None:
    username = username.strip().removeprefix("@")
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("Введите username нового бота из BotFather, без пробелов.")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        raise ValueError("Формат токена не подходит. Скопируйте только сам токен из BotFather, без текста вокруг.")
    target = Path(__file__).resolve().parent / ".env"
    target.write_text(f"BOT_TOKEN={token}\nBOT_USERNAME={username}\n", encoding="utf-8")


def main() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("MONKEYS CARDS LAB — настройка токена")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=22)
    frame.grid()
    ttk.Label(frame, text="Подключение НОВОГО тестового бота", font=("Segoe UI", 12, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
    ttk.Label(frame, text="Username нового бота (без @):").grid(row=1, column=0, columnspan=2, sticky="w")
    username = tk.StringVar()
    username_entry = ttk.Entry(frame, textvariable=username, width=58)
    username_entry.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 12))
    ttk.Label(frame, text="Токен нового бота из BotFather:").grid(row=3, column=0, columnspan=2, sticky="w")
    token = tk.StringVar()
    token_entry = ttk.Entry(frame, textvariable=token, show="*", width=58)
    token_entry.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 6))
    count = tk.StringVar(value="Вставлено символов: 0")
    token.trace_add("write", lambda *_: count.set(f"Вставлено символов: {len(token.get())}"))
    ttk.Label(frame, textvariable=count).grid(row=5, column=0, sticky="w")

    def paste_token() -> None:
        try:
            value = root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("Буфер пуст", "Сначала скопируйте токен в BotFather.", parent=root)
            return
        token.set(value.strip())
        token_entry.focus_set()

    ttk.Button(frame, text="Вставить из буфера", command=paste_token).grid(
        row=5, column=1, sticky="e", padx=(12, 0))
    visible = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame, text="Показать токен на экране", variable=visible,
                    command=lambda: token_entry.configure(show="" if visible.get() else "*")).grid(
        row=6, column=0, columnspan=2, sticky="w", pady=(8, 12))
    ttk.Label(frame, text="Сохраняется только в .env этой папки. Токен в чат не отправляйте.").grid(
        row=7, column=0, columnspan=2, sticky="w")

    def save() -> None:
        try:
            save_settings(username.get(), token.get())
        except ValueError as error:
            messagebox.showerror("Проверьте поля", str(error), parent=root)
            return
        except OSError:
            messagebox.showerror("Ошибка сохранения", "Не удалось записать .env в папке LAB.", parent=root)
            return
        messagebox.showinfo("Готово", "Сохранено в локальный .env.\nТеперь можно запустить start.cmd.", parent=root)
        root.destroy()

    ttk.Button(frame, text="Сохранить", command=save).grid(row=8, column=1, sticky="e", pady=(18, 0))
    root.bind("<Return>", lambda _: save())
    username_entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print field values, a traceback, or clipboard contents.
        print(f"Не удалось открыть настройку: {type(error).__name__}. Заполните .env в Блокноте по README.md.")
