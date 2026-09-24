import os
def clean_go_strm_files(media_dir):
    for root, _, files in os.walk(media_dir):
        for f in files:
            if f.endswith('.strm'):
                path = os.path.join(root, f)
                try:
                    with open(path, 'r') as file:
                        content = file.read()
                    if '8084' in content or 'apx_' in content:
                        os.remove(path)
                        print(f"Removed old Go STRM: {path}")
                except Exception as e:
                    pass
