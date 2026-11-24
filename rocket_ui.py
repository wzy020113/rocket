"""Shared charcoal desktop styling for both transfer applications."""
import tkinter as tk
import customtkinter as ctk

BG = '#20232d'
SURFACE = '#282c37'
SURFACE_2 = '#303543'
SURFACE_3 = '#242833'
BORDER = '#383e50'
BORDER_BRIGHT = '#454d64'
TEXT = '#f0f1f7'
TEXT_DIM = '#adb3c8'
MUTED = '#858da8'
ACCENT = '#7785ff'
ACCENT_HOVER = '#939dff'
ACCENT_2 = '#68d3bc'
WARN = '#ebc080'
DANGER = '#d7798f'
INPUT = '#20242e'
HEADER_LEFT = '#20232d'
HEADER_RIGHT = '#2f3048'
HEADER_GLOW = '#a28ae9'


class ProductHeader(ctk.CTkFrame):
    def __init__(self, parent, title, subtitle, badge, mark='TX'):
        super().__init__(parent, fg_color=BG, corner_radius=0, height=104)
        self.grid_propagate(False)
        self.columnconfigure(1, weight=1)
        icon = tk.Canvas(self, width=52, height=52, bg=BG, highlightthickness=0)
        icon.grid(row=0, column=0, rowspan=2, padx=(26, 16), pady=22)
        icon.create_oval(3, 3, 49, 49, outline=ACCENT, width=2)
        icon.create_polygon(16, 33, 24, 16, 36, 12, 33, 25, 16, 33,
                            fill='', outline=HEADER_GLOW, width=2)
        icon.create_oval(26, 18, 31, 23, outline=ACCENT_2, width=2)
        icon.create_line(15, 37, 21, 31, fill=ACCENT_2, width=2)
        ctk.CTkLabel(self, text=title, text_color=TEXT,
                     font=('Microsoft YaHei UI', 24, 'bold'), anchor='w').grid(
                         row=0, column=1, sticky='sw', pady=(20, 0))
        ctk.CTkLabel(self, text=subtitle, text_color=TEXT_DIM,
                     font=('Microsoft YaHei UI', 11), anchor='w').grid(
                         row=1, column=1, sticky='nw', pady=(3, 18))
        self.status_frame = ctk.CTkFrame(self, fg_color=SURFACE_2,
                                        corner_radius=18, border_width=1, border_color=BORDER)
        self.status_frame.grid(row=0, column=2, rowspan=2, padx=26, pady=28)
        self.status_dot = ctk.CTkLabel(self.status_frame, text='●', text_color=ACCENT_2, width=15)
        self.status_dot.pack(side='left', padx=(12, 4), pady=7)
        self.status_label = ctk.CTkLabel(self.status_frame, text=badge, text_color=TEXT_DIM,
                                       font=('Microsoft YaHei UI', 10))
        self.status_label.pack(side='left', padx=(0, 14), pady=7)
        ctk.CTkFrame(self, height=1, fg_color=BORDER).grid(row=2, column=0, columnspan=3, sticky='ew')

    def set_badge(self, text, color=ACCENT_2):
        self.status_label.configure(text=text)
        self.status_dot.configure(text_color=color)


def add_footer(parent, role):
    from app_version import VERSION
    bar = ctk.CTkFrame(parent, height=28, fg_color=BG, corner_radius=0)
    bar.pack(side='bottom', fill='x', padx=22, pady=(0, 8))
    ctk.CTkLabel(bar, text=f'小火箭  /  {role}  v{VERSION}', text_color=MUTED,
                 font=('Microsoft YaHei UI', 10)).pack(side='left')
    settings = ctk.CTkButton(bar, text='更新地址', width=70, height=26, fg_color='transparent',
                            text_color=MUTED, hover_color=SURFACE_2)
    settings.pack(side='right', padx=(6, 0))
    update = ctk.CTkButton(bar, text='检查更新', width=100, height=26, fg_color=SURFACE_2,
                          text_color=ACCENT_HOVER, hover_color=BORDER)
    update.pack(side='right', padx=6)
    return update, settings
