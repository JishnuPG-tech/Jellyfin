package probe

import (
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"apex/internal/db"
)

type FFprobeOutput struct {
	Streams []struct {
		CodecType    string `json:"codec_type"`
		CodecName    string `json:"codec_name"`
		Profile      string `json:"profile"`
		Width        int    `json:"width"`
		Height       int    `json:"height"`
		RFrameRate   string `json:"r_frame_rate"`
		BitsPerRaw   string `json:"bits_per_raw_sample"`
		PixFmt       string `json:"pix_fmt"`
		ColorSpace   string `json:"color_space"`
		ColorTransfer string `json:"color_transfer"`
		Channels     int    `json:"channels"`
	} `json:"streams"`
	Format struct {
		FormatName string `json:"format_name"`
		BitRate    string `json:"bit_rate"`
	} `json:"format"`
}

func Analyze(ctx context.Context, streamURL string, mediaID string, rawFilename string) (*db.MediaCapability, error) {
	// Locate ffprobe binary
	ffprobePath := "/usr/lib/jellyfin-ffmpeg/ffprobe"
	if _, err := exec.LookPath(ffprobePath); err != nil {
		if path, err2 := exec.LookPath("ffprobe"); err2 == nil {
			ffprobePath = path
		}
	}

	probeCtx, cancel := context.WithTimeout(ctx, 25*time.Second)
	defer cancel()

	cmd := exec.CommandContext(probeCtx, ffprobePath,
		"-v", "quiet",
		"-print_format", "json",
		"-show_format",
		"-show_streams",
		"-analyzeduration", "8M",
		"-probesize", "8M",
		streamURL,
	)

	output, err := cmd.Output()
	ext := strings.ToLower(filepath.Ext(rawFilename))

	cap := &db.MediaCapability{
		MediaID:        mediaID,
		Container:      ext,
		DirectPlaySafe: true,
	}

	if err != nil {
		// Fallback: heuristic inspection if ffprobe failed or timed out
		cap.DirectPlaySafe = (ext == ".mp4" || ext == ".mkv" || ext == ".webm")
		if !cap.DirectPlaySafe {
			cap.TranscodeReason = "Container requires remux or transcode"
		}
		return cap, fmt.Errorf("ffprobe error: %w", err)
	}

	var data FFprobeOutput
	if err := json.Unmarshal(output, &data); err != nil {
		return cap, err
	}

	var subTypes []string

	for _, s := range data.Streams {
		switch s.CodecType {
		case "video":
			if cap.VideoCodec == "" {
				cap.VideoCodec = s.CodecName
				cap.VideoProfile = s.Profile
				cap.Width = s.Width
				cap.Height = s.Height

				// FPS calculation
				if parts := strings.Split(s.RFrameRate, "/"); len(parts) == 2 {
					num, _ := strconv.ParseFloat(parts[0], 64)
					den, _ := strconv.ParseFloat(parts[1], 64)
					if den > 0 {
						cap.FPS = num / den
					}
				}

				// Bit depth
				if s.BitsPerRaw != "" {
					cap.BitDepth, _ = strconv.Atoi(s.BitsPerRaw)
				} else if strings.Contains(s.PixFmt, "10") {
					cap.BitDepth = 10
				} else {
					cap.BitDepth = 8
				}

				// HDR detection
				if strings.Contains(s.ColorTransfer, "smpte2084") {
					cap.HDR = "HDR10"
				} else if strings.Contains(s.ColorTransfer, "arib-std-b67") {
					cap.HDR = "HLG"
				}
			}
		case "audio":
			if cap.AudioCodec == "" {
				cap.AudioCodec = s.CodecName
				cap.AudioChannels = s.Channels
			}
		case "subtitle":
			subTypes = append(subTypes, s.CodecName)
		}
	}

	if len(subTypes) > 0 {
		cap.SubtitleTypes = strings.Join(subTypes, ",")
	}

	if data.Format.BitRate != "" {
		br, _ := strconv.Atoi(data.Format.BitRate)
		cap.Bitrate = br
	}

	// Determine DirectPlay compatibility
	vCodec := strings.ToLower(cap.VideoCodec)
	aCodec := strings.ToLower(cap.AudioCodec)

	isSafeVideo := vCodec == "h264" || vCodec == "hevc" || vCodec == "av1" || vCodec == "vp9"
	isSafeAudio := aCodec == "aac" || aCodec == "mp3" || aCodec == "opus" || aCodec == "flac" || aCodec == "ac3" || aCodec == "eac3"
	isSafeContainer := ext == ".mp4" || ext == ".mkv" || ext == ".webm"

	if !isSafeVideo {
		cap.DirectPlaySafe = false
		cap.TranscodeReason = fmt.Sprintf("Unsupported video codec '%s' for direct play", vCodec)
	} else if !isSafeAudio {
		cap.DirectPlaySafe = false
		cap.TranscodeReason = fmt.Sprintf("Audio codec '%s' may require audio direct-stream transcode", aCodec)
	} else if !isSafeContainer {
		cap.DirectPlaySafe = false
		cap.TranscodeReason = fmt.Sprintf("Container '%s' requires container remuxing", ext)
	}

	return cap, nil
}
