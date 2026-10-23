#!/bin/bash
# DutyCall Rally Video Compressor — iOS Compatible (H.264)
# Usage: ./compress_for_ios.sh "My Rally Folder"

if [ $# -ne 1 ]; then
  echo "Usage: $0 dirName"
  exit 1
fi

dirName="$1"
output_dir="$dirName (iOS Compressed)"

if [ ! -d "$dirName" ]; then
  echo "Error: Directory '$dirName' does not exist."
  exit 1
fi

mkdir -p "$output_dir"

echo "🚀 Compressing for iOS (H.264) — saving to '$output_dir'"

for file in "$dirName"/*; do
  if [ ! -f "$file" ]; then
    continue
  fi

  base=$(basename "$file")
  ext_lower="${base##*.}"
  ext_lower="${ext_lower,,}"

  if [ "$ext_lower" = "mp4" ]; then
    echo "Compressing: $base → H.264"
    
    # Optimized for iOS: H.264 + faststart + good quality/size balance
    ffmpeg -i "$file" \
           -c:v libx264 \
           -preset slow \
           -crf 24 \
           -c:a aac \
           -b:a 128k \
           -movflags +faststart \
           "$output_dir/$base" >/dev/null 2>&1

    if [ $? -eq 0 ]; then
      echo "✅ Compressed: $base"
    else
      echo "❌ Error compressing: $base"
    fi
  else
    # Copy images and other files unchanged
    cp "$file" "$output_dir/$base"
    echo "Copied: $base"
  fi
done

echo "🎉 Processing complete! iOS-ready files are in '$output_dir'"