package com.example.intent;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.view.View;
import android.widget.*;
import android.content.Intent;
import android.net.Uri;
public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        Button btn = findViewById(R.id.btn);
        btn.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                Intent ob = new Intent(Intent.ACTION_SENDTO,Uri.parse("mailto:charante153624@gmail.com"));
                startActivity(ob);
            }
        });
    }


}