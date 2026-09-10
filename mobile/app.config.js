// app.json yerine app.config.js kullanilir - cunku statik JSON, EAS Build
// ortamindaki process.env degiskenlerini okuyamaz. google-services.json
// bilerek git'e commit'lenmiyor (Firebase proje bilgisi), bu yuzden EAS'a
// "GOOGLE_SERVICES_JSON" adinda dosya tipinde bir ortam degiskeni olarak
// yuklendi (bkz. README icindeki eas env:set komutu) - build sirasinda EAS
// bu degiskeni gecici bir dosya yoluna cozup buraya enjekte eder. Yerel
// gelistirmede (eas env:set ile yuklemeden once) hala ./google-services.json
// diskteki dosyaya duser.
module.exports = {
  expo: {
    name: 'Momentum Scanner',
    slug: 'momentum-scanner',
    version: '1.0.0',
    orientation: 'portrait',
    icon: './assets/icon.png',
    userInterfaceStyle: 'dark',
    backgroundColor: '#0f1115',
    android: {
      package: 'com.momentumscanner.app',
      adaptiveIcon: {
        backgroundColor: '#0f1115',
        foregroundImage: './assets/android-icon-foreground.png',
        backgroundImage: './assets/android-icon-background.png',
        monochromeImage: './assets/android-icon-monochrome.png',
      },
      predictiveBackGestureEnabled: false,
      googleServicesFile: process.env.GOOGLE_SERVICES_JSON ?? './google-services.json',
      permissions: ['android.permission.POST_NOTIFICATIONS'],
    },
    web: {
      favicon: './assets/favicon.png',
    },
    plugins: [
      'expo-dev-client',
      [
        'expo-notifications',
        {
          icon: './assets/android-icon-foreground.png',
          color: '#e8c547',
        },
      ],
    ],
    extra: {
      eas: {
        projectId: 'b40201f7-ff06-4118-8afb-0d90be816898',
      },
    },
    owner: 'memo-can',
  },
};
