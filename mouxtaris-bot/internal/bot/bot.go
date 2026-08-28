package bot

import (
	"context"
	"log"

	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
	resolve "mouxtaris.com/dispatcher/internal/resolver"
	"mouxtaris.com/dispatcher/internal/store"
)

type Bot struct {
	api      *tgbotapi.BotAPI
	store    store.UserRepository
	resolver *resolve.Resolver
}

func New(api *tgbotapi.BotAPI, s store.UserRepository, r *resolve.Resolver) *Bot {
	return &Bot{api: api, store: s, resolver: r}
}

func (b *Bot) Run(ctx context.Context) {
	u := tgbotapi.NewUpdate(0)
	u.Timeout = 60
	updates := b.api.GetUpdatesChan(u)

	log.Printf("bot: polling for updates")
	for {
		select {
		case <-ctx.Done():
			b.api.StopReceivingUpdates()
			return
		case update, ok := <-updates:
			if !ok {
				return
			}
			b.handleUpdate(ctx, update)
		}
	}
}

func (b *Bot) send(chatID int64, text string) {
	msg := tgbotapi.NewMessage(chatID, text)
	msg.ParseMode = tgbotapi.ModeHTML
	if _, err := b.api.Send(msg); err != nil {
		log.Printf("bot: send to %d: %v", chatID, err)
	}
}

func (b *Bot) sendWithKeyboard(chatID int64, text string, kb tgbotapi.InlineKeyboardMarkup) {
	msg := tgbotapi.NewMessage(chatID, text)
	msg.ParseMode = tgbotapi.ModeHTML
	msg.ReplyMarkup = kb
	if _, err := b.api.Send(msg); err != nil {
		log.Printf("bot: send keyboard to %d: %v", chatID, err)
	}
}

// editToText replaces the text of an existing message and strips its
// keyboard, so a button press updates the message in place instead of
// leaving stale, still-clickable buttons behind.
func (b *Bot) editToText(chatID int64, messageID int, text string) {
	edit := tgbotapi.NewEditMessageTextAndMarkup(chatID, messageID, text,
		tgbotapi.InlineKeyboardMarkup{InlineKeyboard: [][]tgbotapi.InlineKeyboardButton{}})
	edit.ParseMode = tgbotapi.ModeHTML
	if _, err := b.api.Send(edit); err != nil {
		log.Printf("bot: edit message chat=%d msg=%d: %v", chatID, messageID, err)
	}
}

func (b *Bot) answerCallback(id string) {
	cb := tgbotapi.NewCallback(id, "")
	if _, err := b.api.Request(cb); err != nil {
		log.Printf("bot: answer callback %s: %v", id, err)
	}
}
