import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import Menu
import csv
from datetime import datetime
from urllib.parse import urlparse

class ShoppingListApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Shopping List")
        self.root.geometry("800x400")

        self.items = []
        self.current_file = None

        self.item_frame = tk.Frame(self.root)
        self.item_frame.pack(pady=10)

        self.add_item_button = tk.Button(self.item_frame, text="Add Item", command=self.add_item)
        self.add_item_button.pack(side=tk.LEFT, padx=5)

        self.save_button = tk.Button(self.item_frame, text="Save", command=self.save_items)
        self.save_button.pack(side=tk.LEFT)

        self.load_button = tk.Button(self.item_frame, text="Load", command=self.load_items)
        self.load_button.pack(side=tk.LEFT, padx=5)

        self.item_listbox = tk.Listbox(self.root, width=100, height=15)
        self.item_listbox.pack(pady=10)

        self.item_listbox.bind("<Button-3>", self.show_context_menu)
        self.item_listbox.bind("<Double-Button-1>", self.show_item_details)

    def add_item(self):
        add_item_window = tk.Toplevel(self.root)
        add_item_window.title("Add Item")
        add_item_window.protocol("WM_DELETE_WINDOW", add_item_window.destroy)

        link_label = tk.Label(add_item_window, text="Link:")
        link_label.pack()
        self.link_entry = tk.Entry(add_item_window, width=50)
        self.link_entry.pack()

        nickname_label = tk.Label(add_item_window, text="Nickname:")
        nickname_label.pack()
        self.nickname_entry = tk.Entry(add_item_window, width=50)
        self.nickname_entry.pack()

        cost_label = tk.Label(add_item_window, text="Cost:")
        cost_label.pack()
        self.cost_entry = tk.Entry(add_item_window, width=50)
        self.cost_entry.pack()

        add_button = tk.Button(add_item_window, text="Add", command=self.save_item(add_item_window))
        add_button.pack(pady=10)

    def save_item(self, add_item_window):
        def save_and_close():
            link = self.link_entry.get()
            nickname = self.nickname_entry.get()
            cost = self.cost_entry.get()

            if link and nickname and cost:
                item = [link, nickname, cost, datetime.now()]
                self.items.append(item)
                self.item_listbox.insert(tk.END, item[1])
                self.link_entry.delete(0, tk.END)
                self.nickname_entry.delete(0, tk.END)
                self.cost_entry.delete(0, tk.END)
                add_item_window.destroy()

        return save_and_close

    def format_item(self, item):
        lines = []
        lines.append(f"Name: {item[1]}")
        lines.append(f"Link: {self.get_formatted_link(item[0])}")
        lines.append(f"Price: ${float(item[2]):.2f}")
        lines.append(f"Date Added: {item[3]}")
        return "\n".join(lines) + "\n"

    def get_formatted_link(self, link):
        max_length = 50
        if len(link) > max_length:
            formatted_link = link[:max_length] + "..."
        else:
            formatted_link = link
        return formatted_link

    def save_items(self):
        if self.current_file:
            with open(self.current_file, "w", newline="") as file:
                writer = csv.writer(file)
                writer.writerows(self.items)
        else:
            self.save_as()

    def save_as(self):
        save_file = filedialog.asksaveasfilename(defaultextension=".shp", filetypes=[("Shopping List Files", "*.shp")])
        if save_file:
            self.current_file = save_file
            with open(self.current_file, "w", newline="") as file:
                writer = csv.writer(file)
                writer.writerows(self.items)

    def load_items(self):
        load_file = filedialog.askopenfilename(filetypes=[("Shopping List Files", "*.shp")])
        if load_file:
            self.current_file = load_file
            self.items = []
            self.item_listbox.delete(0, tk.END)
            with open(load_file, "r") as file:
                reader = csv.reader(file)
                for row in reader:
                    item = [row[0], row[1], row[2], row[3]]
                    self.items.append(item)
                    self.item_listbox.insert(tk.END, item[1])

    def show_context_menu(self, event):
        if not self.item_listbox.curselection():
            return

        item_index = self.item_listbox.curselection()[0]
        item = self.items[item_index]

        context_menu = Menu(self.root, tearoff=0)
        context_menu.add_command(label="Delete", command=lambda: self.delete_item(item_index))

        context_menu.post(event.x_root, event.y_root)

    def delete_item(self, item_index):
        confirmed = messagebox.askyesno("Delete Item", "Are you sure you want to delete this item?")
        if confirmed:
            self.item_listbox.delete(item_index)
            del self.items[item_index]
            self.save_items()

    def show_item_details(self, event):
        if not self.item_listbox.curselection():
            return

        item_index = self.item_listbox.curselection()[0]
        item = self.items[item_index]

        details_window = tk.Toplevel(self.root)
        details_window.title("Item Details")

        name_label = tk.Label(details_window, text=f"Name: {item[1]}")
        name_label.pack()

        link_label = tk.Label(details_window, text=f"Link: {item[0]}")
        link_label.pack()

        price_label = tk.Label(details_window, text=f"Price: ${float(item[2]):.2f}")
        price_label.pack()

        date_label = tk.Label(details_window, text=f"Date Added: {item[3]}")
        date_label.pack()

        copy_name_button = tk.Button(details_window, text="Copy Name", command=lambda: self.copy_to_clipboard(item[1]))
        copy_name_button.pack(side=tk.LEFT)

        copy_link_button = tk.Button(details_window, text="Copy Link", command=lambda: self.copy_to_clipboard(item[0]))
        copy_link_button.pack(side=tk.LEFT)

        copy_price_button = tk.Button(details_window, text="Copy Price", command=lambda: self.copy_to_clipboard(str(float(item[2]))))
        copy_price_button.pack(side=tk.LEFT)

        copy_date_button = tk.Button(details_window, text="Copy Date", command=lambda: self.copy_to_clipboard(item[3]))
        copy_date_button.pack(side=tk.LEFT)

    def copy_to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        messagebox.showinfo("Copy to Clipboard", "Copied to clipboard!")

root = tk.Tk()
app = ShoppingListApp(root)
root.mainloop()
