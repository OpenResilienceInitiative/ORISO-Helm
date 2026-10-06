package main

import (
	"crypto/rand"
	"fmt"
	"net/http"
	"sync"
	"time"
)

const (
	cookieName = "anon-session"
	sessionTTL = 30 * time.Minute
	cleanupInt = 5 * time.Minute
)

type entry struct {
	uuid    string
	expires time.Time
}

var (
	mu       sync.RWMutex
	sessions = map[string]*entry{}
)

func uuid() string {
	b := make([]byte, 16)
	rand.Read(b)
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func auth(w http.ResponseWriter, r *http.Request) {
	var anonId string

	cookie, err := r.Cookie(cookieName)
	if err == nil {
		mu.Lock()
		if e, ok := sessions[cookie.Value]; ok && time.Now().Before(e.expires) {
			e.expires = time.Now().Add(sessionTTL)
			anonId = e.uuid
		}
		mu.Unlock()
	}

	if anonId == "" {
		token := uuid()
		anonId = uuid()
		mu.Lock()
		sessions[token] = &entry{uuid: anonId, expires: time.Now().Add(sessionTTL)}
		mu.Unlock()
		http.SetCookie(w, &http.Cookie{
			Name:     cookieName,
			Value:    token,
			Path:     "/",
			HttpOnly: true,
			Secure:   true,
			MaxAge:   int(sessionTTL.Seconds()),
			SameSite: http.SameSiteStrictMode,
		})
	}

	w.Header().Set("X-Anon-Id", anonId)
	w.WriteHeader(http.StatusOK)
}

func cleanup() {
	for {
		time.Sleep(cleanupInt)
		now := time.Now()
		mu.Lock()
		for k, e := range sessions {
			if now.After(e.expires) {
				delete(sessions, k)
			}
		}
		mu.Unlock()
	}
}

func main() {
	go cleanup()
	http.HandleFunc("/auth", auth)
	http.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	http.ListenAndServe(":8080", nil)
}
