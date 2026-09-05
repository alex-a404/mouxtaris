package outages

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
)

// LoadFile restores known outages from a prior SaveFile call, so a process
// restart doesn't forget everything it had already announced. A missing file
// (first run) isn't an error.
func (s *Service) LoadFile(path string) error {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}

	var known map[string]knownOutage
	if err := json.Unmarshal(data, &known); err != nil {
		return err
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	s.known = known
	return nil
}

// SaveFile writes the current known outages to disk, replacing the file
// atomically (write + rename) so a crash mid-write can't leave a truncated
// file behind for the next LoadFile to choke on.
func (s *Service) SaveFile(path string) error {
	s.mu.Lock()
	data, err := json.Marshal(s.known)
	s.mu.Unlock()
	if err != nil {
		return err
	}

	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// EnsureDir creates the directory a state file will live in, if needed.
func EnsureDir(path string) error {
	return os.MkdirAll(filepath.Dir(path), 0o755)
}
