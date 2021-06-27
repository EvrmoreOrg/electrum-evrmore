from electrum import assets

script = bytes.fromhex('76a91476063d8481adc7577e93e3d13e11de230cea7d8788acc02072766e7110454c45435452554d5f54455354494e47000052acdfb2241d00010075')

data = assets.pull_meta_from_create_or_reissue_script(script)

print(data)
