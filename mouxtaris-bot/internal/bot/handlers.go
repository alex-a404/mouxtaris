package bot

import (
	"context"
	"fmt"
	"log"
	"strings"

	tgbotapi "github.com/go-telegram-bot-api/telegram-bot-api/v5"
)

func (b *Bot) handleUpdate(ctx context.Context, u tgbotapi.Update) {
	switch {
	case u.Message != nil && u.Message.IsCommand():
		b.handleCommand(ctx, u.Message)
	case u.CallbackQuery != nil:
		b.handleCallback(ctx, u.CallbackQuery)
	}
}

func (b *Bot) handleCommand(ctx context.Context, msg *tgbotapi.Message) {
	switch msg.Command() {
	case "start":
		b.cmdStart(ctx, msg)
	case "subscribe":
		b.cmdSubscribe(ctx, msg)
	case "unsubscribe":
		b.cmdUnsubscribe(ctx, msg)
	case "list":
		b.cmdList(ctx, msg)
	case "delete_me":
		b.cmdDeleteMe(ctx, msg)
	}
}

func (b *Bot) handleCallback(ctx context.Context, cb *tgbotapi.CallbackQuery) {
	b.answerCallback(cb.ID)
	chatID := cb.Message.Chat.ID

	msgID := cb.Message.MessageID

	switch {
	case cb.Data == "cancel":
		b.editToText(chatID, msgID, "Cancelled.")

	case strings.HasPrefix(cb.Data, "sub:"):
		areaKey := strings.TrimPrefix(cb.Data, "sub:")
		if err := b.store.Subscribe(ctx, chatID, areaKey); err != nil {
			log.Printf("bot: subscribe chat=%d area=%q: %v", chatID, areaKey, err)
			b.editToText(chatID, msgID, "Something went wrong. Please try again.")
			return
		}
		// Find a display name for confirmation
		name := b.resolver.DisplayName(areaKey)
		if name == "" {
			name = areaKey
		}
		confirmation := fmt.Sprintf("Subscribed to <b>%s</b>. You'll be notified of power cuts there.", name)
		if dk := b.resolver.DistrictKey(areaKey); dk == "larnaka" || dk == "ammochostos" {
			confirmation += "\n\nNote: water outage alerts aren't available for Larnaca/Famagusta yet — only power cuts."
		}
		b.editToText(chatID, msgID, confirmation)

	case strings.HasPrefix(cb.Data, "unsub:"):
		areaKey := strings.TrimPrefix(cb.Data, "unsub:")
		if err := b.store.Unsubscribe(ctx, chatID, areaKey); err != nil {
			log.Printf("bot: unsubscribe chat=%d area=%q: %v", chatID, areaKey, err)
			b.editToText(chatID, msgID, "Something went wrong. Please try again.")
			return
		}
		b.editToText(chatID, msgID, "Subscription removed.")

	case cb.Data == "delete_me:confirm":
		if err := b.store.DeleteUser(ctx, chatID); err != nil {
			log.Printf("bot: delete user chat=%d: %v", chatID, err)
			b.editToText(chatID, msgID, "Something went wrong. Please try again.")
			return
		}
		b.editToText(chatID, msgID, "Your data has been deleted. Send /start if you'd like to use the bot again.")
	}
}

func (b *Bot) cmdStart(ctx context.Context, msg *tgbotapi.Message) {
	if err := b.store.UpsertUser(ctx, msg.Chat.ID); err != nil {
		log.Printf("bot: upsert user chat=%d: %v", msg.Chat.ID, err)
		b.send(msg.Chat.ID, "Something went wrong. Please try again.")
		return
	}
	b.send(msg.Chat.ID,
		"Welcome to <b>Mouxtaris</b>!\n\n"+
			"Get notified when power cuts affect your area in Cyprus.\n\n"+
			"/subscribe &lt;area&gt; — add an area (e.g. /subscribe Strovolos)\n"+
			"/list — view your subscriptions\n"+
			"/unsubscribe — remove a subscription\n"+
			"/delete_me — delete your account and all your data\n\n"+
			"Privacy: the service stores only your Telegram ID and the areas you pick. No name, address, or location.\n"+
			"Issues or suggestions? Contact mouxtarisbot@proton.me",
	)
}

func (b *Bot) cmdSubscribe(ctx context.Context, msg *tgbotapi.Message) {
	query := strings.TrimSpace(msg.CommandArguments())
	if query == "" {
		b.send(msg.Chat.ID, "Please provide an area name.\nExample: /subscribe Strovolos")
		return
	}

	if err := b.store.UpsertUser(ctx, msg.Chat.ID); err != nil {
		log.Printf("bot: upsert user chat=%d: %v", msg.Chat.ID, err)
		b.send(msg.Chat.ID, "Something went wrong. Please try again.")
		return
	}

	results := b.resolver.Search(query, 5)
	if len(results) == 0 {
		b.send(msg.Chat.ID, fmt.Sprintf("No areas found matching <b>%s</b>. Try a different spelling.", query))
		return
	}

	var rows [][]tgbotapi.InlineKeyboardButton
	for _, r := range results {
		label := r.NameEN
		if r.District != "" && r.District != r.NameEN {
			label = fmt.Sprintf("%s (%s)", r.NameEN, r.District)
		}
		data := "sub:" + r.Key
		if len(data) > 64 {
			continue // key too long for Telegram callback data; skip
		}
		rows = append(rows, tgbotapi.NewInlineKeyboardRow(
			tgbotapi.NewInlineKeyboardButtonData(label, data),
		))
	}
	rows = append(rows, tgbotapi.NewInlineKeyboardRow(
		tgbotapi.NewInlineKeyboardButtonData("Cancel", "cancel"),
	))

	b.sendWithKeyboard(msg.Chat.ID,
		fmt.Sprintf("Found %d result(s) for <b>%s</b>. Choose one:", len(results), query),
		tgbotapi.NewInlineKeyboardMarkup(rows...),
	)
}

func (b *Bot) cmdUnsubscribe(ctx context.Context, msg *tgbotapi.Message) {
	subs, err := b.store.ListSubscriptions(ctx, msg.Chat.ID)
	if err != nil {
		log.Printf("bot: list subscriptions chat=%d: %v", msg.Chat.ID, err)
		b.send(msg.Chat.ID, "Something went wrong. Please try again.")
		return
	}
	if len(subs) == 0 {
		b.send(msg.Chat.ID, "You have no active subscriptions.")
		return
	}

	var rows [][]tgbotapi.InlineKeyboardButton
	for _, s := range subs {
		data := "unsub:" + s.AreaKey
		if len(data) > 64 {
			continue
		}
		rows = append(rows, tgbotapi.NewInlineKeyboardRow(
			tgbotapi.NewInlineKeyboardButtonData("Remove: "+s.NameEN, data),
		))
	}

	b.sendWithKeyboard(msg.Chat.ID, "Select a subscription to remove:", tgbotapi.NewInlineKeyboardMarkup(rows...))
}

func (b *Bot) cmdList(ctx context.Context, msg *tgbotapi.Message) {
	subs, err := b.store.ListSubscriptions(ctx, msg.Chat.ID)
	if err != nil {
		log.Printf("bot: list subscriptions chat=%d: %v", msg.Chat.ID, err)
		b.send(msg.Chat.ID, "Something went wrong. Please try again.")
		return
	}
	if len(subs) == 0 {
		b.send(msg.Chat.ID, "You have no active subscriptions.\nUse /subscribe &lt;area&gt; to add one.")
		return
	}

	var sb strings.Builder
	sb.WriteString("<b>Your subscriptions:</b>\n")
	for _, s := range subs {
		fmt.Fprintf(&sb, "• %s\n", s.NameEN)
	}
	b.send(msg.Chat.ID, sb.String())
}

func (b *Bot) cmdDeleteMe(ctx context.Context, msg *tgbotapi.Message) {
	kb := tgbotapi.NewInlineKeyboardMarkup(
		tgbotapi.NewInlineKeyboardRow(
			tgbotapi.NewInlineKeyboardButtonData("Delete my data", "delete_me:confirm"),
			tgbotapi.NewInlineKeyboardButtonData("Cancel", "cancel"),
		),
	)
	b.sendWithKeyboard(msg.Chat.ID,
		"This will delete your account and all your subscriptions. This cannot be undone.",
		kb,
	)
}
