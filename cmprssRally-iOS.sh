#!/bin/bash
# DutyCall Rally Video Compressor — iOS Compatible (H.264)
# Usage:
#   ./compress_for_ios.sh                → Compresses ALL rally folders in current directory
#   ./compress_for_ios.sh "Jeeves"       → Compresses only the specified folder

if [ $# -gt 1 ]; then
  echo "Usage: $0 [optional_rally_folder]"
  echo "   (no argument = compress all rally folders in current dir)"
  exit 1
fi

# If a folder was provided, process only that one (original behavior)
if [ $# -eq 1 ]; then
  folders=("$1")
else
  # No argument → find all subdirectories (rally folders) in current directory
  echo "🔍 No folder specified — compressing ALL rally folders in current directory..."
  folders=()
  for dir in */; do
    if [ -d "$dir" ]; then
      folders+=("${dir%/}")  # remove trailing slash
    fi
  done
fi

for dirName in "${folders[@]}"; do
  output_dir="$dirName (iOS Compressed)"

  if [ ! -d "$dirName" ]; then
    echo "⚠️  Skipping: '$dirName' is not a directory"
    continue
  fi

  mkdir -p "$output_dir"

  echo "🚀 Processing rally: $dirName → $output_dir"

  for file in "$dirName"/*; do
    if [ ! -f "$file" ]; then
      continue
    fi

    base=$(basename "$file")
    ext_lower="${base##*.}"
    ext_lower="${ext_lower,,}"

    if [ "$ext_lower" = "mp4" ]; then
      echo "   Compressing: $base"

      ffmpeg -i "$file" \
             -c:v libx264 \
             -preset slow \
             -crf 24 \
             -c:a aac \
             -b:a 128k \
             -movflags +faststart \
             "$output_dir/$base" >/dev/null 2>&1

      if [ $? -eq 0 ]; then
        echo "   ✅ Compressed: $base"
      else
        echo "   ❌ Error compressing: $base"
      fi
    else
      # Copy images and other files unchanged
      cp "$file" "$output_dir/$base"
      echo "   Copied: $base"
    fi
  done
done

echo "🎉 All done! iOS-ready compressed rallies are ready to upload to S3."
